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
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()

from aiohttp.web import Application, Request, Response, json_response, middleware, run_app

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

from openai import OpenAI

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
# Preferred auth: Entra identity (az login locally, managed identity in Azure)
# via AIProjectClient. Fallback: the Foundry account API key (FOUNDRY_API_KEY)
# for environments where nobody can grant the identity a data-plane role yet.
FOUNDRY_API_KEY = os.environ.get("FOUNDRY_API_KEY", "")

# Name of the AgentApplication auth handler (AGENTAPPLICATION__USERAUTHORIZATION__
# HANDLERS__<name>__...) used to get each Teams user's own token for Foundry.
# Required for per-user Fabric/RLS behavior — see README "Per-user identity".
FOUNDRY_AUTH_HANDLER = os.environ.get("FOUNDRY_AUTH_HANDLER_NAME", "FOUNDRY")

# Only attach auth_handlers to the message route once the handler is actually
# configured (its OAuth connection setting present) — asking the SDK to route
# through a handler that doesn't exist is untested territory and this must not
# risk breaking a bot that today has no OAuth connection wired up at all.
_FOUNDRY_AUTH_CONFIGURED = bool(os.environ.get(
    f"AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__{FOUNDRY_AUTH_HANDLER}"
    "__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME"
))


def _shared_client() -> OpenAI:
    """OFFLINE TESTS ONLY — never serves live Teams traffic. Used by
    test_foundry_pipeline.py / tests/test_build_reply.py, which have no
    signed-in user. The live message path is per-user only (see on_message);
    this shared identity must never handle a real caller's turn, or every user
    would see one identity's Fabric data instead of their own."""
    if FOUNDRY_API_KEY:
        return OpenAI(
            base_url=f"{FOUNDRY_PROJECT_ENDPOINT.rstrip('/')}/openai/v1",
            api_key=FOUNDRY_API_KEY,
            default_headers={"api-key": FOUNDRY_API_KEY},
        )
    project = AIProjectClient(
        endpoint=FOUNDRY_PROJECT_ENDPOINT,
        credential=DefaultAzureCredential(),
    )
    return project.get_openai_client()


def _user_client(user_token: str) -> OpenAI:
    """Client scoped to one Teams user's own Entra token — required so tools
    with identity passthrough (Fabric data agent) enforce THAT user's row/table
    level security, matching direct-publish behavior. See the Fabric tool docs:
    service-principal/API-key auth is explicitly unsupported for that tool."""
    return OpenAI(
        base_url=f"{FOUNDRY_PROJECT_ENDPOINT.rstrip('/')}/openai/v1",
        api_key=user_token,
    )


# Built at import for the offline test harness only. The live path constructs a
# per-user client each turn (see on_message) and never touches this — it must
# not, or one identity's data would leak across users. See _shared_client.
openai_client = _shared_client()
log.info("Shared/offline Foundry client built (%s) — NOT used for live turns",
         "account API key" if FOUNDRY_API_KEY else "Entra identity (DefaultAzureCredential)")

# One Foundry conversation per signed-in USER (not per Teams thread) — matches
# how direct-publish scopes a session to the signed-in identity, and means the
# same person gets the same context across 1:1 and group chats. In-memory =
# demo only; swap for durable storage (task 6) before anything real.
_conversations: dict[str, str] = {}


def _run_agent_sync(user_key: str, user_text: str, client: OpenAI):
    """Send user_text to the agent inside a persistent conversation; return the Response."""
    conv_id = _conversations.get(user_key)
    if not conv_id:
        conversation = client.conversations.create()
        conv_id = conversation.id
        _conversations[user_key] = conv_id
        log.info("created conversation %s for user %s", conv_id, user_key)

    response = client.responses.create(
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
    for item in response.output:
        itype = getattr(item, "type", "?")
        if itype == "message":
            for block in item.content or []:
                anns = getattr(block, "annotations", None) or []
                log.info("  message block type=%s annotations=%d %s",
                         getattr(block, "type", "?"), len(anns),
                         [(getattr(a, "type", "?"), getattr(a, "filename", "?")) for a in anns])
        else:
            log.info("  output item type=%s", itype)
    return response


def _download_container_file_sync(client: OpenAI, container_id: str, file_id: str) -> bytes:
    """The core 'do what the playground does' step: fetch output bytes by id."""
    content = client.containers.files.content.retrieve(
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


def build_reply(response, client: OpenAI = None) -> Activity:
    """One Teams activity: cleaned text + inline images + file download buttons.

    On the new surface every generated file (chart PNGs included) arrives as a
    container_file_citation annotation on an output_text content block.
    `client` must be the SAME client (same identity) used for the responses.create
    call, so file downloads honor the same per-user access as the agent run.
    """
    client = client or openai_client
    text_parts: list[str] = []
    citations: dict[str, object] = {}  # file_id -> annotation, deduped
    container_ids: list[str] = []
    raw_texts: list[str] = []

    for item in response.output:
        itype = getattr(item, "type", None)
        if itype == "code_interpreter_call" and getattr(item, "container_id", None):
            container_ids.append(item.container_id)
        if itype != "message":
            continue
        for block in item.content or []:
            if getattr(block, "type", None) != "output_text":
                continue
            if block.text:
                raw_texts.append(block.text)
                text_parts.append(_strip_sandbox_links(block.text))
            for ann in block.annotations or []:
                if getattr(ann, "type", None) == "container_file_citation":
                    citations.setdefault(ann.file_id, ann)

    # The model sometimes writes a sandbox link without emitting the citation
    # annotation (observed in practice). Recover those files by name from the
    # code interpreter container listing.
    cited_names = {ann.filename for ann in citations.values()}
    orphan_names = {
        m.split("/")[-1]
        for t in raw_texts
        for m in _SANDBOX_BARE.findall(t)
    } - cited_names
    if orphan_names and container_ids:
        for cid in container_ids:
            for cf in client.containers.files.list(container_id=cid):
                name = (getattr(cf, "path", "") or "").split("/")[-1]
                if name in orphan_names and cf.id not in citations:
                    log.info("recovered uncited file %s (%s) from container %s", cf.id, name, cid)
                    citations[cf.id] = SimpleNamespace(
                        type="container_file_citation",
                        file_id=cf.id, container_id=cid, filename=name,
                    )
                    orphan_names.discard(name)
    if orphan_names:
        log.warning("files mentioned in text but not recoverable: %s", sorted(orphan_names))

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
        data = _download_container_file_sync(client, ann.container_id, ann.file_id)
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
# connection_manager MUST be passed here too (not only to the adapter): the SDK
# hands it to the OAuth/OBO handlers as their _connection_manager. Without it the
# on-behalf-of exchange hits `'NoneType' object has no attribute 'get_connection'`.
AGENT_APP = AgentApplication[TurnState](
    storage=MemoryStorage(), adapter=ADAPTER,
    connection_manager=CONNECTION_MANAGER, **sdk_config
)


RELAY_CLIENT_ID = os.environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "")

_inflight: set[str] = set()  # activity ids being processed (guards channel retries)


def _user_key(context: TurnContext) -> str:
    """Stable per-user id: prefer the Entra object id (stable across chats/
    tenexchanges), fall back to the channel's own user id (e.g. DirectLine
    testing with no signed-in AAD identity)."""
    frm = context.activity.from_property
    return getattr(frm, "aad_object_id", None) or frm.id


async def _process_and_reply(reference, user_key: str, user_text: str, client: OpenAI):
    """Runs after the turn was ack'd: call the agent, then deliver proactively."""
    try:
        response = await asyncio.to_thread(_run_agent_sync, user_key, user_text, client)
        reply = await asyncio.to_thread(build_reply, response, client)
    except Exception as e:
        log.exception("agent processing failed for user %s", user_key)
        reply = Activity(type=ActivityTypes.message,
                         text=f"Sorry, something went wrong: {e}")

    async def _send(ctx: TurnContext):
        await ctx.send_activity(reply)

    await ADAPTER.continue_conversation(
        RELAY_CLIENT_ID, reference.get_continuation_activity(), _send
    )


@AGENT_APP.activity(
    ActivityTypes.message,
    auth_handlers=[FOUNDRY_AUTH_HANDLER] if _FOUNDRY_AUTH_CONFIGURED else None,
)
async def on_message(context: TurnContext, _state: TurnState):
    user_text = (context.activity.text or "").strip()
    user_key = _user_key(context)
    activity_id = context.activity.id or ""
    if not user_text:
        return
    # Channels redeliver activities they consider unacknowledged; don't run
    # the agent twice for the same activity id.
    if activity_id and activity_id in _inflight:
        log.info("duplicate delivery of activity %s ignored", activity_id)
        return
    if activity_id:
        _inflight.add(activity_id)

    # Per-user identity is MANDATORY (README "Per-user identity / RLS"): with
    # auth_handlers set on this route, the SDK completes Teams sign-in (OAuthCard)
    # + OBO exchange before this handler runs, and get_token returns THAT user's
    # token. We NEVER fall back to a shared identity — serving one caller under
    # another's identity would leak data across users and defeat Fabric per-user
    # RLS. If no user token is available, refuse the turn.
    try:
        token_response = await AGENT_APP.auth.get_token(context, FOUNDRY_AUTH_HANDLER)
    except Exception:
        log.exception("token retrieval failed for %s; refusing turn", user_key)
        token_response = None
    if not (token_response and token_response.token):
        log.warning("no per-user token for %s; refusing turn (no shared fallback)", user_key)
        if activity_id:
            _inflight.discard(activity_id)
        await context.send_activity(
            "I couldn't verify your identity, so I can't access your data. "
            "Please complete the sign-in prompt and try again."
        )
        return
    client = _user_client(token_response.token)

    await context.send_activity(Activity(type=ActivityTypes.typing))
    reference = context.activity.get_conversation_reference()

    async def _runner():
        try:
            await _process_and_reply(reference, user_key, user_text, client)
        finally:
            _inflight.discard(activity_id)

    # ack the turn immediately; the reply goes out proactively when ready
    asyncio.create_task(_runner())


# ---------------------------------------------------------------------------
# HTTP hosting
# ---------------------------------------------------------------------------
async def entry_point(req: Request) -> Response:
    return await start_agent_process(req, AGENT_APP, ADAPTER)


async def health(_req: Request) -> Response:
    return json_response({"status": "ok", "agent": FOUNDRY_AGENT_NAME or "(unset)"})


@middleware
async def _auth_except_health(request: Request, handler):
    if request.path == "/healthz":
        return await handler(request)
    return await jwt_authorization_middleware(request, handler)


APP = Application(middlewares=[_auth_except_health])
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
    # Per-user identity is a hard requirement — there is no shared-identity
    # fallback on the live path. Refuse to start if the FOUNDRY OAuth connection
    # isn't wired, so a misconfigured deploy fails loudly here instead of booting
    # "healthy" and refusing every user's turn. See .env.example / README.
    if not _FOUNDRY_AUTH_CONFIGURED:
        raise SystemExit(
            "Per-user identity is required but no FOUNDRY OAuth connection is "
            "configured. Set AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__"
            f"{FOUNDRY_AUTH_HANDLER}__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME "
            "(plus OBOCONNECTIONNAME + SCOPES) — see .env.example / README "
            "'Per-user identity / RLS'."
        )
    run_app(APP, host="0.0.0.0", port=int(os.environ.get("PORT", 3978)))
