"""
Foundry Agent -> Teams relay
============================
Delivers Azure AI Foundry agent (Code Interpreter) outputs into Microsoft Teams:
inline charts (Adaptive Card images) and real file-download links (blob + SAS).

  Teams -> Azure Bot Service -> (messaging endpoint) -> THIS APP -> Foundry Agent

Point the Azure Bot resource's messaging endpoint at:
  https://<this-app-host>/api/messages

Verified against (pinned in requirements.txt, Python 3.11):
  azure-ai-agents 1.1.0, azure-ai-projects 2.3.0, botbuilder-* 4.17.1
Note: azure-ai-projects 2.x dropped `.agents`; the classic threads/runs surface
lives in azure-ai-agents' AgentsClient, constructed directly with the project
endpoint. All method names below were checked against the installed package.
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

from aiohttp import web
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity, ActivityTypes, Attachment

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import ListSortOrder, MessageRole, ThreadMessage
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
class BotConfig:
    """Read by ConfigurationBotFrameworkAuthentication / the credential factory."""

    APP_ID = os.environ.get("MicrosoftAppId", "")
    APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")
    APP_TYPE = os.environ.get("MicrosoftAppType", "MultiTenant")  # or SingleTenant / UserAssignedMSI
    APP_TENANTID = os.environ.get("MicrosoftAppTenantId", "")


FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_AGENT_ID = os.environ.get("FOUNDRY_AGENT_ID", "")

BLOB_CONN_STR = os.environ.get("BLOB_CONN_STR", "")
BLOB_CONTAINER = os.environ.get("BLOB_CONTAINER", "agent-files")
SAS_TTL_HOURS = int(os.environ.get("SAS_TTL_HOURS", "1"))

# Teams rejects oversized activities; large chart PNGs can't ride inline as a
# base64 data URI. Above this many raw bytes we fall back to a blob+SAS image URL.
DATA_URI_MAX_BYTES = int(os.environ.get("DATA_URI_MAX_BYTES", "20000"))


# ---------------------------------------------------------------------------
# Foundry agent client (classic threads/runs surface, azure-ai-agents 1.1.0)
# ---------------------------------------------------------------------------
agents = AgentsClient(
    endpoint=FOUNDRY_PROJECT_ENDPOINT,
    credential=DefaultAzureCredential(),
)

# One agent thread per Teams conversation. In-memory = demo only (task 6: durable).
_threads: dict[str, str] = {}


def _run_agent_sync(conversation_id: str, user_text: str) -> ThreadMessage | None:
    """Send user_text to the agent, wait for the run, return the newest assistant message."""
    thread_id = _threads.get(conversation_id)
    if not thread_id:
        thread = agents.threads.create()
        thread_id = thread.id
        _threads[conversation_id] = thread_id
        log.info("created thread %s for conversation %s", thread_id, conversation_id)

    agents.messages.create(thread_id=thread_id, role=MessageRole.USER, content=user_text)

    run = agents.runs.create_and_process(thread_id=thread_id, agent_id=FOUNDRY_AGENT_ID)
    log.info("run %s finished: %s", run.id, run.status)
    if run.status == "failed":
        raise RuntimeError(f"Agent run failed: {run.last_error}")
    if run.status == "requires_action":
        # Code Interpreter never requires action; this appears only if other
        # (function) tools are added to the agent later.
        raise RuntimeError("Run requires tool action; this relay does not handle function tools yet.")

    for m in agents.messages.list(thread_id=thread_id, order=ListSortOrder.DESCENDING):
        if m.role == MessageRole.AGENT:
            return m
    return None


def _download_file_sync(file_id: str) -> bytes:
    """The core 'do what the playground does' step: fetch output bytes by file_id."""
    return b"".join(agents.files.get_content(file_id))


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


def _image_card(data: bytes) -> Attachment:
    if len(data) <= DATA_URI_MAX_BYTES or not BLOB_CONN_STR:
        url = f"data:image/png;base64,{base64.b64encode(data).decode()}"
        if len(data) > DATA_URI_MAX_BYTES:
            log.warning(
                "image is %d bytes (> %d) and no BLOB_CONN_STR set; "
                "sending as data URI, Teams may reject the activity",
                len(data), DATA_URI_MAX_BYTES,
            )
    else:
        url = _upload_to_blob_sync(data, "chart.png")
    return _adaptive_card([{"type": "Image", "url": url}])


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


def build_reply(message: ThreadMessage) -> Activity:
    """One Teams activity: cleaned text + inline images + download buttons."""
    attachments: list[Attachment] = []

    for img in message.image_contents:
        data = _download_file_sync(img.image_file.file_id)
        log.info("image output %s: %d bytes", img.image_file.file_id, len(data))
        attachments.append(_image_card(data))

    seen: set[str] = set()
    for ann in message.file_path_annotations:
        file_id = ann.file_path.file_id
        if file_id in seen:
            continue
        seen.add(file_id)
        filename = ann.text.split("/")[-1] if ann.text else f"{file_id}.dat"
        data = _download_file_sync(file_id)
        log.info("file output %s (%s): %d bytes", file_id, filename, len(data))
        attachments.append(_file_download_card(data, filename))

    text_parts = [_strip_sandbox_links(t.text.value) for t in message.text_messages]
    reply_text = "\n\n".join(p for p in text_parts if p)
    if not reply_text and attachments:
        reply_text = "Here you go:"

    return Activity(type=ActivityTypes.message, text=reply_text, attachments=attachments)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class RelayBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        user_text = (turn_context.activity.text or "").strip()
        conv_id = turn_context.activity.conversation.id
        if not user_text:
            return
        try:
            await turn_context.send_activity(
                Activity(type=ActivityTypes.typing)
            )
            msg = await asyncio.to_thread(_run_agent_sync, conv_id, user_text)
            if msg is None:
                await turn_context.send_activity("No response from the agent.")
                return
            reply = await asyncio.to_thread(build_reply, msg)
            await turn_context.send_activity(reply)
        except Exception as e:
            log.exception("failed handling message in conversation %s", conv_id)
            await turn_context.send_activity(f"Sorry, something went wrong: {e}")


ADAPTER = CloudAdapter(ConfigurationBotFrameworkAuthentication(BotConfig()))
BOT = RelayBot()


async def messages(req: web.Request) -> web.Response:
    return await ADAPTER.process(req, BOT)


async def health(_req: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "agent": FOUNDRY_AGENT_ID or "(unset)"})


APP = web.Application()
APP.router.add_post("/api/messages", messages)
APP.router.add_get("/healthz", health)

if __name__ == "__main__":
    missing = [k for k, v in {
        "FOUNDRY_PROJECT_ENDPOINT": FOUNDRY_PROJECT_ENDPOINT,
        "FOUNDRY_AGENT_ID": FOUNDRY_AGENT_ID,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)} (see .env.example)")
    web.run_app(APP, host="0.0.0.0", port=int(os.environ.get("PORT", 3978)))
