# Foundry → Teams Connector

Relay that delivers Azure AI Foundry agent (Code Interpreter) outputs into
Microsoft Teams: **inline charts** and **working file-download links** — the two
things the Foundry "Publish to Teams" wizard does not deliver (dead
`sandbox:/mnt/data/...` links, missing images).

```
Teams -> Azure Bot Service -> (messaging endpoint) -> THIS APP -> Foundry Agent
                                                        |
                                          downloads outputs by file_id and
                                          re-delivers as Teams attachments
```

> Full research and evidence for why the built-in publish path can't do this:
> see **[RESEARCH.md](RESEARCH.md)**.

## Status of the gap (verified against official docs, 2026-07-13)

- **No no-code fix exists.** The Foundry publish flow's own limitations table
  still excludes generated files/images; the Tech Community thread on this
  (Feb–Jul 2026) has no Microsoft fix. Copilot Studio's code-interpreter FAQ
  explicitly says images are *not* rendered in the Teams channel. A custom
  relay is the supported production pattern.
- **The classic Agents API (threads/runs, `asst_...` ids) is retiring.**
  Assistants infrastructure retires **2026-08-26**; Foundry classic agents
  **2027-03-31**, but Microsoft says don't rely on the later date. New Foundry
  portal agents use the **Responses/conversations surface** via
  `AIProjectClient.get_openai_client()` — that's what `app.py` targets.
- **Bot Framework SDK (`botbuilder-*`) is retired** (support ended 2025-12-31).
  `app.py` uses the GA replacement, the **M365 Agents SDK**
  (`microsoft-agents-*` 1.1.0).
- **Teams bot message limit is ~100 KB** and base64 card images are excluded
  from the count (the old 28 KB limit applies to webhooks/connectors, not
  bots). Data-URI images in cards are still not formally documented, so large
  images fall back to blob+SAS URLs.

## Files

| File | Purpose |
|---|---|
| `app.py` | The relay. New Foundry surface (agent referenced **by name**) + M365 Agents SDK hosting. |
| `app_classic.py` | Fallback for classic `asst_...` agents (azure-ai-agents 1.1.0 + botbuilder CloudAdapter). Dead end after 2026-08-26. |
| `foundry_teams_relay.py` | Original reference implementation (pre-verification, kept for comparison). |
| `CLAUDE_CODE_HANDOFF.md` | Project brief / task list. |

## How outputs are delivered

On the new surface every generated file — chart PNGs included — arrives as a
`container_file_citation` annotation (`file_id` + `container_id` + `filename`)
on an `output_text` block. The relay downloads each immediately (Code
Interpreter containers expire after 1 h active / 30 min idle), then:

- **Images** (`.png/.jpg/...`): Adaptive Card `Image` — inline base64 data URI
  when ≤ `DATA_URI_MAX_BYTES`, otherwise uploaded to blob and referenced by a
  short-lived SAS URL.
- **Other files**: uploaded to blob, presented as an Adaptive Card with a
  **Download** button (`Action.OpenUrl` + SAS URL). Works in all Teams scopes,
  unlike `FileConsentCard` (1:1 only).
- `sandbox:` links are rewritten out of the reply text.

## Run locally

```powershell
python -m venv .venv            # Python 3.11
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # fill in values
.\.venv\Scripts\python app.py   # listens on :3978
```

Expose it with a dev tunnel and point the Azure Bot's messaging endpoint at it:

```powershell
devtunnel host -p 3978 --allow-anonymous
# Azure Bot resource -> Configuration -> Messaging endpoint:
#   https://<tunnel-host>/api/messages
```

The identity running the relay (`DefaultAzureCredential`: your `az login`
locally, managed identity in Azure) needs access to the Foundry project
(e.g. **Azure AI User** role), or file downloads return 401.

### End-to-end test prompts (in Teams)

1. "Create a CSV of 12 months of sales and give me the file." → Download button.
2. "Plot that as a bar chart." → chart renders inline.

## Adapting this to your own agent / subscription

Nothing in the relay is specific to one agent — everything is config. Checklist:

1. **Prereqs**: Python 3.11, `pip install -r requirements.txt`, Azure CLI
   (pip-installable, see `.venv-azcli` pattern / `run_local.ps1`).
2. **Entra**: one app registration (single tenant, no API permissions) +
   client secret. This is the bot's identity.
3. **`.env`** (copy `.env.example`): your Foundry project endpoint + agent
   NAME; the app registration's client id/secret/tenant; a storage-account
   connection string for download links.
4. **Foundry access**: the identity running the relay (your `az login` user
   locally; managed identity in production) needs the **Azure AI User** role
   on the Foundry project — otherwise file downloads return 401.
5. **Azure Bot**: `.\create_bot.ps1 -AppId <client-id> -TunnelHost <host>`
   (creates the bot, enables the Teams channel, builds `teams-app/foundry-relay.zip`).
   Note: bot handles are globally unique — pass `-BotName`.
6. **Validate the Foundry leg without Teams**: `.\run_local.ps1 test_foundry_pipeline.py`.
7. **Teams**: upload/approve the zip (org catalog, one-time Teams admin
   approval), start relay + tunnel (`start_all.ps1`), chat.

End users need **no Azure access of any kind** — see "Security model" below.

## Security model (who needs access to what)

| Principal | Needs |
|---|---|
| Relay's identity (dev: your user; prod: managed identity) | Azure AI User on the Foundry project; write access to the storage account |
| Bot app registration | Nothing beyond existing — it only authenticates bot traffic |
| Teams end users | **Nothing.** The Teams admin makes the app available; users add it and chat. File downloads work via short-lived SAS URLs embedded in the card — the link itself is the authorization (`SAS_TTL_HOURS`, default 1 h). Users never touch Foundry, Blob, or Azure. |

## Known limitations / next steps

- Conversation map (Teams conversation → Foundry conversation) is in-memory —
  restarts drop context. Task 6: durable storage
  (`microsoft-agents-storage-blob` or Cosmos).
- No streaming (the publish flow doesn't support it either).
- `extra_body={"agent_reference": {...}}` and the containers download path are
  from the official migration/code-interpreter docs (July 2026) but not yet
  exercised against a live project — first live run should confirm both.
- Production hosting decision pending (App Service vs Container App).

## Pinned versions

Recorded in `requirements.txt`; verified by introspection on 2026-07-13 against
Python 3.11.9. Key pins: `azure-ai-projects 2.3.0` (note: 2.x removed
`AIProjectClient.agents`), `microsoft-agents-* 1.1.0`.
