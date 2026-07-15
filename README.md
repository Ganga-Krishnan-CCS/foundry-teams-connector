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

## Per-user identity / RLS (Fabric data agent)

If the agent has a **Fabric data agent** tool, this matters: Fabric enforces
row/table-level security only for a request made with a **signed-in user's own
token** — its docs explicitly say service-principal/API-key auth is not
supported. A relay calling Foundry under one shared identity (the default
setup above) makes every Teams user see that one identity's data — this was
observed directly (two users, same 5-table result) before the fix below.

Direct-publish avoids this by making each user complete a one-time Foundry
sign-in (via a Microsoft-managed OAuth broker) before their first tool call.
`app.py` replicates the same effect using the M365 Agents SDK's built-in OAuth
+ on-behalf-of (OBO) support:

- `on_message` is registered with `auth_handlers=["FOUNDRY"]`, so the SDK
  handles the sign-in card + OBO token exchange before the handler runs.
- The exchanged user token builds a **per-request** OpenAI client
  (`_user_client`); that same client is used for the agent call, the file
  downloads, and orphan-file recovery, so everything in one turn is
  consistently scoped to that user.
- The conversation map is now keyed by **user** (Entra object id), not by
  Teams thread — matching direct-publish's per-identity session and giving
  each person their own context across 1:1 and group chats.
- If no token is available (auth not configured yet, or a test harness with
  no signed-in user), it falls back to the shared identity and logs a warning
  — same single-identity behavior as before, not a crash.

**Setup** (blocked on an Entra admin — same shape as the bot app registration
ask): an Azure Bot Service OAuth connection (`Aadv2` provider) on the bot's app
registration, granted delegated **`user_impersonation`** on the first-party
resource **"Azure Machine Learning Services"** (appId
`18a66f5f-dbdf-4c17-9dd7-1634712a9cbe`) — adding that API permission and
consenting to it requires Application Administrator/Global Administrator (a
Contributor cannot). Once granted:

```powershell
az ad app permission add --id <bot-app-id> --api 18a66f5f-dbdf-4c17-9dd7-1634712a9cbe --api-permissions 1a7925b5-f871-417a-9b8b-303f9f29fa10=Scope
az ad app permission admin-consent --id <bot-app-id>
az bot authsetting create -n <bot-name> -g <rg> -c FoundryAAD --client-id <bot-app-id> --client-secret <bot-app-secret> --service Aadv2 --provider-scope-string "18a66f5f-dbdf-4c17-9dd7-1634712a9cbe/user_impersonation"
```

Then set the three `AGENTAPPLICATION__USERAUTHORIZATION__...` values in
`.env` (see `.env.example`) to `FoundryAAD` / the scope above, and redeploy.

**Status: implemented, not yet live-verified** — blocked on the admin-consent
step above. Once granted, validate with the original two-user test (different
Fabric table access) through the real bot before relying on it.

## Known limitations / next steps

- Conversation map (Teams conversation → Foundry conversation) is in-memory —
  restarts drop context. Task 6: durable storage
  (`microsoft-agents-storage-blob` or Cosmos).
- No streaming (the publish flow doesn't support it either).
- ~~`agent_reference` / containers download unverified~~ — verified live
  2026-07-14: pipeline test, DirectLine, and the real Teams client all working.
- User-uploaded files (attachments sent TO the bot) are ignored.
- SAS download links expire after `SAS_TTL_HOURS` (default 1 h) — a policy
  decision for production; old blobs are never cleaned up.
- Production hosting: recommended App Service (Linux, Always On, managed
  identity) — see "Production deployment" below.

## Production deployment (App Service)

1. Create a Linux App Service (B1+, **Always On**) with a **system-assigned
   managed identity**; deploy this repo; startup command `python app.py`.
2. Grant the managed identity a Foundry data-plane role — role names vary by
   tenant: **Azure AI User** where it exists, else the pair
   **Foundry User** + **Azure AI Developer** (scope: the AI Services account).
   Requires Owner/User Access Administrator; a Contributor cannot assign roles.
   **Interim fallback** if no Owner is available: set `FOUNDRY_API_KEY` (the
   AI account key, readable by Contributors) — the relay then talks to the
   project's `/openai/v1` surface with key auth. Swap to managed identity
   later by granting the roles and deleting the `FOUNDRY_API_KEY` setting.
3. Set the `.env` values as App Service **application settings** (the
   `CONNECTIONS__...__CLIENTSECRET` ideally as a Key Vault reference).
4. Point the Azure Bot's messaging endpoint at
   `https://<app>.azurewebsites.net/api/messages` — the dev tunnel is no
   longer involved.
5. Before scaling beyond one instance: replace the in-memory conversation map
   and inflight-dedupe set with durable storage (blob/Cosmos) — they are
   per-process state.

## Pinned versions

Recorded in `requirements.txt`; verified by introspection on 2026-07-13 against
Python 3.11.9. Key pins: `azure-ai-projects 2.3.0` (note: 2.x removed
`AIProjectClient.agents`), `microsoft-agents-* 1.1.0`.
