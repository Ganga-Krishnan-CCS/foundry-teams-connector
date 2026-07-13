"""
Foundry Agent -> Teams relay (reference / demo bot)
====================================================
Purpose: prove out "file download" and "image display" from an Azure AI Foundry
agent (with Code Interpreter) directly inside Microsoft Teams.

This is the piece that the out-of-the-box "Publish to Teams" flow does NOT do:
it retrieves code-interpreter file outputs by file_id and re-delivers them to
Teams as things Teams can actually render (inline image + downloadable link).

How it fits in:
  Teams  ->  Azure Bot Service  ->  (messaging endpoint)  ->  THIS APP  ->  Foundry Agent
Point your existing Azure Bot resource's Messaging endpoint at:
  https://<this-app-host>/api/messages

------------------------------------------------------------------------------
IMPORTANT - verify before running:
  * The azure-ai-agents / azure-ai-projects SDK surface has changed across
    preview versions. The method names used below (threads.create,
    messages.create, runs.create_and_process, messages.list, files.get_content,
    and the .image_contents / .file_path_annotations helper properties) match
    a recent version - if yours differs, adjust the ~5 marked calls. The
    STRUCTURE is what matters and won't change.
  * pip install: aiohttp botbuilder-core azure-ai-projects azure-identity
                 azure-storage-blob
------------------------------------------------------------------------------
"""

import os
import sys
import time
import base64
import datetime
import traceback

from aiohttp import web
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
    ActivityHandler,
)
from botbuilder.schema import Activity, Attachment

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
)

# ---------------------------------------------------------------------------
# Config (set these as environment / App Service application settings)
# ---------------------------------------------------------------------------
MICROSOFT_APP_ID = os.environ.get("MicrosoftAppId", "")
MICROSOFT_APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

FOUNDRY_PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]  # your project endpoint
FOUNDRY_AGENT_ID = os.environ["FOUNDRY_AGENT_ID"]                  # the gpt-5 agent's id

# Blob storage is used to hand Teams a real, clickable download URL for files.
# (Images don't need this - they go inline. See _deliver_outputs below.)
BLOB_CONN_STR = os.environ.get("BLOB_CONN_STR", "")
BLOB_CONTAINER = os.environ.get("BLOB_CONTAINER", "agent-files")

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
project = AIProjectClient(
    endpoint=FOUNDRY_PROJECT_ENDPOINT,
    credential=DefaultAzureCredential(),
)
agents = project.agents  # AgentsClient

# One thread per Teams conversation (in-memory = fine for a demo, not prod).
_threads: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Core: run the agent and collect its outputs
# ---------------------------------------------------------------------------
def run_agent(conversation_id: str, user_text: str):
    """Send user_text to the agent, wait for the run, return the assistant message."""
    thread_id = _threads.get(conversation_id)
    if not thread_id:
        thread = agents.threads.create()          # [SDK] create thread
        thread_id = thread.id
        _threads[conversation_id] = thread_id

    agents.messages.create(                        # [SDK] add user message
        thread_id=thread_id, role="user", content=user_text
    )

    run = agents.runs.create_and_process(          # [SDK] run + poll to terminal
        thread_id=thread_id, agent_id=FOUNDRY_AGENT_ID
    )
    if run.status == "failed":
        raise RuntimeError(f"Agent run failed: {run.last_error}")

    # newest assistant message
    msgs = agents.messages.list(thread_id=thread_id, order="desc")  # [SDK] list
    for m in msgs:
        if m.role == "assistant":
            return m
    return None


def download_file_bytes(file_id: str) -> bytes:
    """Pull the actual bytes for a code-interpreter output by file_id."""
    chunks = agents.files.get_content(file_id)     # [SDK] returns byte iterator
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Delivery: turn agent outputs into things Teams can render
# ---------------------------------------------------------------------------
def _image_card(data: bytes) -> Attachment:
    """Inline image via an Adaptive Card using a data URI (no hosting needed)."""
    b64 = base64.b64encode(data).decode()
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "Image", "url": f"data:image/png;base64,{b64}"}],
    }
    return Attachment(
        content_type="application/vnd.microsoft.card.adaptive", content=card
    )


def _file_download_card(data: bytes, filename: str) -> Attachment:
    """Upload the file to blob, mint a short-lived SAS URL, offer it as a button.
    SAS-link works in every Teams scope (1:1, group, channel). The alternative -
    a FileConsentCard that lands the file in the user's OneDrive - only works in
    1:1 chats, so blob is the safer demo choice."""
    svc = BlobServiceClient.from_connection_string(BLOB_CONN_STR)
    try:
        svc.create_container(BLOB_CONTAINER)
    except Exception:
        pass  # already exists

    blob_name = f"{int(time.time())}-{filename}"
    svc.get_blob_client(BLOB_CONTAINER, blob_name).upload_blob(data, overwrite=True)

    sas = generate_blob_sas(
        account_name=svc.account_name,
        container_name=BLOB_CONTAINER,
        blob_name=blob_name,
        account_key=svc.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    )
    url = f"{svc.url}{BLOB_CONTAINER}/{blob_name}?{sas}"

    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "TextBlock", "text": f"📄 {filename}", "wrap": True}],
        "actions": [{"type": "Action.OpenUrl", "title": "Download", "url": url}],
    }
    return Attachment(
        content_type="application/vnd.microsoft.card.adaptive", content=card
    )


def build_reply(message) -> Activity:
    """Assemble one Teams activity: text + inline images + file download buttons."""
    attachments: list[Attachment] = []

    # ---- charts / images: image_file content items ----
    for img in getattr(message, "image_contents", []):   # [SDK] helper property
        data = download_file_bytes(img.image_file.file_id)
        attachments.append(_image_card(data))

    # ---- downloadable files: file_path annotations ----
    for ann in getattr(message, "file_path_annotations", []):  # [SDK] helper prop
        file_id = ann.file_path.file_id
        # derive a filename from the sandbox path in the annotation text
        filename = ann.text.split("/")[-1] if ann.text else f"{file_id}.dat"
        data = download_file_bytes(file_id)
        attachments.append(_file_download_card(data, filename))

    # ---- text: strip the useless sandbox: links before sending ----
    text_parts = []
    for t in getattr(message, "text_messages", []):       # [SDK] helper property
        val = t.text.value
        if "sandbox:" not in val:
            text_parts.append(val)
    reply_text = "\n".join(text_parts) or ("Here you go:" if attachments else "")

    return Activity(type="message", text=reply_text, attachments=attachments)


# ---------------------------------------------------------------------------
# Bot plumbing
# ---------------------------------------------------------------------------
class RelayBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext):
        user_text = turn_context.activity.text or ""
        conv_id = turn_context.activity.conversation.id
        try:
            msg = run_agent(conv_id, user_text)
            if msg is None:
                await turn_context.send_activity("No response from the agent.")
                return
            await turn_context.send_activity(build_reply(msg))
        except Exception as e:
            traceback.print_exc()
            await turn_context.send_activity(f"Error: {e}")


SETTINGS = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
ADAPTER = BotFrameworkAdapter(SETTINGS)
BOT = RelayBot()


async def messages(req: web.Request) -> web.Response:
    body = await req.json()
    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")
    response = await ADAPTER.process_activity(
        activity, auth_header, BOT.on_turn
    )
    if response:
        return web.json_response(data=response.body, status=response.status)
    return web.Response(status=201)


APP = web.Application()
APP.router.add_post("/api/messages", messages)

if __name__ == "__main__":
    try:
        web.run_app(APP, host="0.0.0.0", port=int(os.environ.get("PORT", 3978)))
    except Exception as error:
        raise error
