"""
Foundry Agent -> Teams relay (current Foundry surface, mid-2026)
================================================================
Delivers Azure AI Foundry agent (Code Interpreter) outputs into Microsoft Teams:
inline charts (Adaptive Card images) and real file-download links (blob + SAS).
This is the piece the Foundry "Publish to Teams" wizard does not do.

  Teams -> Azure Bot Service -> (messaging endpoint) -> THIS APP -> Foundry Agent

Point the Azure Bot resource's messaging endpoint at:
  https://<this-app-host>/api/messages

Stack (pinned in requirements.txt, Python 3.11):
  * azure-ai-projects 2.3.0 — new Responses/conversations surface. Agents are
    referenced BY NAME via extra_body {"agent_reference": ...}; outputs arrive
    as container_file_citation annotations (file_id + container_id + filename).
    The classic threads/runs surface retires with Assistants on 2026-08-26 —
    see app_classic.py only if your agent is a classic (asst_...) one.
  * microsoft-agents-* 1.1.0 — M365 Agents SDK, the replacement for the retired
    Bot Framework SDK (botbuilder-*).

Code Interpreter containers expire (1 h active / 30 min idle), so output files
are downloaded immediately, within the same turn.
"""

import asyncio
import base64
import datetime
import logging
import os
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from aiohttp.web import Application, Request, Response, json_response, run_app

from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    Attachment,
    load_configuration_from_env,
)
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "")

BLOB_CONN_STR = os.environ.get("BLOB_CONN_STR", "")
BLOB_CONTAINER = os.environ.get("BLOB_CONTAINER", "agent-files")
SAS_TTL_HOURS = int(os.environ.get("SAS_TTL_HOURS", "1"))

# Teams' bot message limit is ~100 KB but base64 card images are excluded from
# the count (learn.microsoft.com "Format your bot messages", 2026). Data-URI
# images are still not formally documented for Teams cards, so above this raw
# size we prefer a blob+SAS image URL when blob storage is configured.
DATA_URI_MAX_BYTES = int(os.environ.get("DATA_URI_MAX_BYTES", "51200"))

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# ---------------------------------------------------------------------------
# Foundry clients (new surface: OpenAI-compatible conversations + responses)
# ---------------------------------------------------------------------------
project = AIProjectClient(
    endpoint=FOUNDRY_PROJECT_ENDPOINT,
    credential=DefaultAzureCredential(),
)
openai_client = project.get_openai_client()

# One Foundry conversation per Teams conversation. In-memory = demo only;
# swap for durable storage (task 6) before anything real.
_conversations: dict[str, str] = {}


def _run_agent_sync(teams_conv_id: str, user_text: str):
    """Send user_text to the agent inside a persistent conversation; return the Response."""
    conv_id = _conversations.get(teams_conv_id)
    if not conv_id:
        conversation = openai_client.conversations.create()
        conv_id = conversation.id
        _conversations[teams_conv_id] = conv_id
        log.info("created conversation %s for Teams conversation %s", conv_id, teams_conv_id)

    response = openai_client.responses.create(
        conversation=conv_id,
        input=user_text,
        extra_body={
            "agent_reference": {
                "name": FOUNDRY_AGENT_NAME,
                "type": "agent_reference",
            }
        },
    )
    log.info("response %s status=%s", response.id, response.status)
    return response


def _download_container_file_sync(container_id: str, file_id: str) -> bytes:
    """The core 'do what the playground does' step: fetch output bytes by id."""
    content = openai_client.containers.files.content.retrieve(
        file_id=file_id, container_id=container_id
    )
    return content.read()


# ---------------------------------------------------------------------------
# Blob delivery (files always; images only when too big for a data URI)
# ---------------------------------------------------------------------------
def _upload_to_blob_sync(data: bytes, filename: str) -> str:
    """Upload bytes and return a short-lived read-only SAS URL."""
    svc = BlobServiceClient.from_connection_string(BLOB_CONN_STR)
    try:
        svc.create_container(BLOB_CONTAINER)
    except Exception:
        pass  # already exists

    blob_name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{filename}"
    blob = svc.get_blob_client(BLOB_CONTAINER, blob_name)
    blob.upload_blob(data, overwrite=True)

    sas = generate_blob_sas(
        account_name=svc.account_name,
        container_name=BLOB_CONTAINER,
        blob_name=blob_name,
        account_key=svc.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=SAS_TTL_HOURS),
    )
    return f"{blob.url}?{sas}"


# ---------------------------------------------------------------------------
# Teams rendering
# ---------------------------------------------------------------------------
def _adaptive_card(body: list, actions: list | None = None) -> Attachment:
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card)


def _image_card(data: bytes, filename: str) -> Attachment:
    if len(data) <= DATA_URI_MAX_BYTES or not BLOB_CONN_STR:
        url = f"data:image/png;base64,{base64.b64encode(data).decode()}"
        if len(data) > DATA_URI_MAX_BYTES:
            log.warning(
                "image %s is %d bytes (> %d) and no BLOB_CONN_STR set; "
                "sending as data URI, Teams may reject or drop it",
                filename, len(data), DATA_URI_MAX_BYTES,
            )
    else:
        url = _upload_to_blob_sync(data, filename)
    return _adaptive_card([{"type": "Image", "url": url, "altText": filename}])


def _file_download_card(data: bytes, filename: str) -> Attachment:
    """Blob + SAS download button. Works in every Teams scope (1:1, group, channel),
    unlike FileConsentCard which is 1:1 only."""
    url = _upload_to_blob_sync(data, filename)
    size_kb = max(1, len(data) // 1024)
    return _adaptive_card(
        body=[{"type": "TextBlock", "text": f"📄 {filename} ({size_kb} KB)", "wrap": True}],
        actions=[{"type": "Action.OpenUrl", "title": "Download", "url": url}],
    )


# matches markdown links pointing at the sandbox, e.g. [sales.csv](sandbox:/mnt/data/sales.csv)
_SANDBOX_MD_LINK = re.compile(r"\[([^\]]*)\]\(sandbox:[^)]*\)")
_SANDBOX_BARE = re.compile(r"sandbox:[^\s)]+")


def _strip_sandbox_links(text: str) -> str:
    text = _SANDBOX_MD_LINK.sub(r"\1", text)
    return _SANDBOX_BARE.sub("", text).strip()


def build_reply(response) -> Activity:
    """One Teams activity: cleaned text + inline images + file download buttons.

    On the new surface every generated file (chart PNGs included) arrives as a
    container_file_citation annotation on an output_text content block.
    """
    text_parts: list[str] = []
    citations: dict[str, object] = {}  # file_id -> annotation, deduped

    for item in response.output:
        if getattr(item, "type", None) != "message":
            continue
        for block in item.content or []:
            if getattr(block, "type", None) != "output_text":
                continue
            if block.text:
                text_parts.append(_strip_sandbox_links(block.text))
            for ann in block.annotations or []:
                if getattr(ann, "type", None) == "container_file_citation":
                    citations.setdefault(ann.file_id, ann)

    # Code Interpreter often cites the same chart twice: the inline render gets
    # an auto-generated name (<file_id>.png) and plt.savefig() a real one. When
    # named images exist, drop the auto-named ones to avoid duplicates in Teams.
    def _is_auto_named(a) -> bool:
        return os.path.splitext(a.filename or "")[0] == a.file_id

    def _is_image(a) -> bool:
        return os.path.splitext(a.filename or "")[1].lower() in IMAGE_EXTENSIONS

    if any(_is_image(a) and not _is_auto_named(a) for a in citations.values()):
        citations = {
            fid: a for fid, a in citations.items()
            if not (_is_image(a) and _is_auto_named(a))
        }

    attachments: list[Attachment] = []
    for ann in citations.values():
        filename = ann.filename or f"{ann.file_id}.dat"
        data = _download_container_file_sync(ann.container_id, ann.file_id)
        ext = os.path.splitext(filename)[1].lower()
        log.info("output file %s (%s): %d bytes", ann.file_id, filename, len(data))
        if ext in IMAGE_EXTENSIONS:
            attachments.append(_image_card(data, filename))
        else:
            attachments.append(_file_download_card(data, filename))

    reply_text = "\n\n".join(p for p in text_parts if p)
    if not reply_text and attachments:
        reply_text = "Here you go:"
    if not reply_text and not attachments:
        reply_text = "The agent returned no content."

    return Activity(type=ActivityTypes.message, text=reply_text, attachments=attachments)


# ---------------------------------------------------------------------------
# Bot (M365 Agents SDK)
# ---------------------------------------------------------------------------
sdk_config = load_configuration_from_env(os.environ)

CONNECTION_MANAGER = MsalConnectionManager(**sdk_config)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
AGENT_APP = AgentApplication[TurnState](
    storage=MemoryStorage(), adapter=ADAPTER, **sdk_config
)


@AGENT_APP.activity(ActivityTypes.message)
async def on_message(context: TurnContext, _state: TurnState):
    user_text = (context.activity.text or "").strip()
    teams_conv_id = context.activity.conversation.id
    if not user_text:
        return
    try:
        await context.send_activity(Activity(type=ActivityTypes.typing))
        response = await asyncio.to_thread(_run_agent_sync, teams_conv_id, user_text)
        reply = await asyncio.to_thread(build_reply, response)
        await context.send_activity(reply)
    except Exception as e:
        log.exception("failed handling message in conversation %s", teams_conv_id)
        await context.send_activity(f"Sorry, something went wrong: {e}")


# ---------------------------------------------------------------------------
# HTTP hosting
# ---------------------------------------------------------------------------
async def entry_point(req: Request) -> Response:
    return await start_agent_process(req, AGENT_APP, ADAPTER)


async def health(_req: Request) -> Response:
    return json_response({"status": "ok", "agent": FOUNDRY_AGENT_NAME or "(unset)"})


APP = Application(middlewares=[jwt_authorization_middleware])
APP.router.add_post("/api/messages", entry_point)
APP.router.add_get("/healthz", health)
APP["agent_configuration"] = CONNECTION_MANAGER.get_default_connection_configuration()

if __name__ == "__main__":
    missing = [k for k, v in {
        "FOUNDRY_PROJECT_ENDPOINT": FOUNDRY_PROJECT_ENDPOINT,
        "FOUNDRY_AGENT_NAME": FOUNDRY_AGENT_NAME,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)} (see .env.example)")
    run_app(APP, host="0.0.0.0", port=int(os.environ.get("PORT", 3978)))
