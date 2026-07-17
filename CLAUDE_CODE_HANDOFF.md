# Project brief: deliver Foundry agent file downloads + inline charts into Microsoft Teams

> **HISTORICAL DOCUMENT (original brief, 2026-07).** The project has since been
> built and live-verified — see **README.md** for current architecture and
> status. Notable ways this brief is superseded: the classic threads/runs API it
> describes (`image_contents`, `file_path_annotations`, `files.get_content`,
> `MicrosoftAppId/Password` config) was replaced by the Responses/`agent_reference`
> surface + M365 Agents SDK; the relay now also does **per-user identity (Teams
> SSO + OBO, fail-closed)** for Fabric RLS, which this brief predates. Task list
> items 1–5 and 7–8 are done; 6 (durable storage) and 9 (product track) remain.

Paste this whole file as your opening prompt in Claude Code, then work through the
task list. It contains everything needed to continue without re-explaining.

---

## Goal
I have an Azure AI Foundry agent (Agent Service) with the **Code Interpreter** tool
enabled. In the Foundry playground, the agent correctly generates downloadable files
(CSV/xlsx) and charts (PNG). When the agent is published to Microsoft Teams via the
Foundry "Publish to Teams" wizard, **file download links are dead and charts never
appear**. I need file download + inline image display working in Teams, built as a
real deliverable I can eventually offer to customers.

## Current state
- Agent: freshly created in Foundry UI, model **gpt-5**, Code Interpreter enabled.
- Published to Teams via the Foundry wizard, which created an **Azure Bot** resource
  + a compute app (App Service or Function App) running a Bot Framework relay.
- Verified (or need to verify) that files/charts DO work in the Foundry playground —
  so the agent is fine; the gap is purely in Teams delivery.

## Root cause (confirmed understanding)
Code Interpreter does not return real URLs. It returns:
- **Charts/images** as `image_file` content items, each holding a `file_id`.
- **Downloadable files** as text with a `file_path` annotation containing a `file_id`;
  the visible text shows a `sandbox:/mnt/data/<name>` string that is NOT a real link.

The Foundry playground is a bespoke client that resolves these: it calls the Files API
with credentials to fetch the bytes, then renders/serves them. Teams is a generic Bot
Framework client that only understands `activity.text` + `activity.attachments`; it has
no ability to reach the Files API. The default wizard relay only forwards text, so the
`sandbox:` string goes out as a dead link and `image_file` bytes are never fetched.

**The fix:** the relay must do what the playground does — download each output by
`file_id` and re-deliver it as Teams-renderable attachments. This is the standard,
Microsoft-supported production pattern for agents in Teams, not a hack. The wizard is
only a quickstart.

## Architecture
Teams -> Azure Bot Service -> (messaging endpoint) -> RELAY APP -> Foundry Agent
The relay is an always-on service (App Service / Function App). Its messaging endpoint
is `https://<host>/api/messages`, set on the Azure Bot resource.

## Delivery mechanics
- **Images**: send inline via an Adaptive Card `Image` element using a `data:image/png;base64,...` URI. No hosting needed. (Watch the size ceiling for very large images.)
- **Files**: upload bytes to Azure Blob Storage, mint a short-lived SAS URL, present a
  "Download" button (Adaptive Card `Action.OpenUrl`). SAS works in all Teams scopes.
  Alternative = Teams `FileConsentCard` (lands file in user's OneDrive) but that ONLY
  works in 1:1 chats, so SAS is the safer default.
- Always strip the `sandbox:` string out of the outgoing text.

## Reference implementation (Python)
A working reference bot exists (aiohttp + botbuilder-core + azure-ai-projects +
azure-storage-blob). Key functions:
- `run_agent()` — create/reuse a thread per conversation, add message, run, return the
  latest assistant message.
- `download_file_bytes(file_id)` — `agents.files.get_content(file_id)` -> bytes. This is
  the core "do what the playground does" step.
- `_image_card(bytes)` — Adaptive Card with a base64 data-URI image.
- `_file_download_card(bytes, name)` — blob upload + SAS + download button.
- `build_reply(message)` — walk `message.image_contents` and
  `message.file_path_annotations`, download each, assemble one activity; strip sandbox
  links from `message.text_messages`.

(If I have the reference .py file, I'll paste it into the repo. Ask me for it if not.)

## IMPORTANT caveats to respect
1. **SDK is version-sensitive.** The `azure-ai-agents` / `azure-ai-projects` surface has
   changed across previews. Method names (`threads.create`, `messages.create`,
   `runs.create_and_process`, `messages.list`, `files.get_content`) and helper properties
   (`.image_contents`, `.file_path_annotations`, `.text_messages`) must be verified
   against the installed package version. Pin versions and confirm each call.
2. **Preview API risk.** Parts of the Agent stack are preview; a production/customer
   offering must plan for maintenance as Microsoft changes things.
3. **Auth.** The relay's identity (`DefaultAzureCredential` -> managed identity) must have
   access to the Foundry project, or `files.get_content` returns 401. This is the auth
   Teams lacks and the relay must supply.
4. **State.** The in-memory thread map in the reference is demo-only; use durable storage
   for anything real.
5. **Check current docs first.** Verify whether newer Foundry / Copilot Studio / the
   M365 Agents SDK have since closed this gap in a no-code path — that would change the
   approach from "build the missing piece" to "configure a supported feature."

## Task list for Claude Code
1. Set up a clean repo + virtualenv; pin and install SDK versions; record exact versions.
2. Confirm the actual method/property names against the installed `azure-ai-agents` /
   `azure-ai-projects` version; adjust the reference calls if they differ.
3. Stand up the relay locally; use a tunnel (e.g. dev tunnel / ngrok) to expose
   `/api/messages`; point a test Azure Bot messaging endpoint at it.
4. Wire config: FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_AGENT_ID, MicrosoftAppId,
   MicrosoftAppPassword, BLOB_CONN_STR, BLOB_CONTAINER.
5. Validate end to end in Teams with two prompts:
   - "Create a CSV of 12 months of sales and give me the file." -> working Download button.
   - "Plot that as a bar chart." -> chart renders inline.
6. Replace the in-memory thread store with durable storage.
7. Add logging + error handling; handle runs that require tool approval or fail.
8. Decide production hosting (App Service vs Function App vs Container App) and deploy.
9. (Product track) Evaluate Copilot Studio or the Teams AI Library as a more supportable
   foundation than a raw custom bot before committing to a customer offering.

## What I want from you (Claude Code)
Start by helping me verify the SDK version and get the reference relay running locally
against my agent, then iterate toward a deployable service. Ask me for the reference .py
file, my endpoint/agent id, and my SDK versions when you need them. Flag any point where
the current Microsoft docs likely supersede the assumptions above.
