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

Note: the app **refuses to start** unless the per-user OAuth settings are
present (see "Per-user identity / RLS") — there is no shared-identity mode on
the live path. For validating the Foundry leg without Teams, use
`test_foundry_pipeline.py`, which uses a shared/offline client
(`FOUNDRY_API_KEY` or `DefaultAzureCredential` with the **Foundry User** role
— formerly named Azure AI User).

### End-to-end test prompts (in Teams)

1. "Create a CSV of 12 months of sales and give me the file." → Download button.
2. "Plot that as a bar chart." → chart renders inline.

## Adapting this to your own agent / subscription

Nothing in the relay is specific to one agent — everything is config. Checklist:

1. **Prereqs**: Python 3.11, `pip install -r requirements.txt`, Azure CLI
   (pip-installable, see `.venv-azcli` pattern / `run_local.ps1`).
2. **Entra**: one app registration (single tenant) + client secret. This is
   the bot's identity. For per-user SSO it also needs: Application ID URI
   `api://botid-<app-id>` exposing an `access_as_user` scope (pre-authorize
   Teams clients `5e3ce6c0-2b1f-4285-8d4b-75ee78787346` and
   `1fec8e78-bce4-4aaf-ab1b-5451cc387264`), and redirect URI
   `https://token.botframework.com/.auth/web/redirect`.
3. **`.env`** (copy `.env.example`): your Foundry project endpoint + agent
   NAME; the app registration's client id/secret/tenant; a storage-account
   connection string for download links.
4. **Foundry access**: live turns run under **each end user's own identity**
   (see "Per-user identity / RLS") — every end user needs a Foundry role
   (below). The relay's own identity (your `az login` user locally; managed
   identity or `FOUNDRY_API_KEY` in production) is only used by the offline
   pipeline test and needs **Foundry User** (formerly Azure AI User) on the
   project.
5. **Azure Bot**: `.\create_bot.ps1 -AppId <client-id> -TunnelHost <host>`
   (creates the bot, enables the Teams channel, builds `teams-app/foundry-relay.zip`).
   Note: bot handles are globally unique — pass `-BotName`.
6. **Validate the Foundry leg without Teams**: `.\run_local.ps1 test_foundry_pipeline.py`.
7. **Teams**: upload/approve the zip (org catalog, one-time Teams admin
   approval), start relay + tunnel (`start_all.ps1`), chat.

## Security model (who needs access to what)

Every live turn is executed **as the signed-in end user** (fail-closed: no
token → the turn is refused). That means end users must be authorized at each
hop — this matches the requirements of Microsoft's native publish path for
Fabric-tool agents (their Fabric tool doc: "Assign developers **and end users**
at least the Foundry User Azure RBAC role").

| Principal | Needs |
|---|---|
| Teams end users | (1) one-time Entra consent to the bot's `access_as_user` (self-serve card in Teams); (2) **Foundry Agent Consumer** at agent scope — or **Foundry User** at project scope — on the Foundry resource; (3) if the agent has a Fabric tool: READ on the Fabric data agent + underlying data-source permissions (this is where RLS applies) and coverage by the Fabric "Copilot and Azure OpenAI" tenant settings (incl. cross-geo for non-EU/US capacities). Manage all three via one security group. |
| Relay's identity (offline test only) | **Foundry User** on the project (or `FOUNDRY_API_KEY`); write access to the storage account for blob/SAS delivery |
| Bot app registration | Delegated `access_as_user` on itself (exposed API); the Bot OAuth connection + OBO exchange run under it |

File downloads are delivered via short-lived SAS URLs embedded in the card
(`SAS_TTL_HOURS`, default 1 h) — users never touch Blob/Azure directly.

## Per-user identity / RLS (Fabric data agent)

If the agent has a **Fabric data agent** tool, this matters: Fabric enforces
row/table-level security only for a request made with a **signed-in user's own
token** — its docs explicitly say service-principal/API-key auth is not
supported. A relay calling Foundry under one shared identity would make every
Teams user see that one identity's data — this was observed directly (two
users, same 5-table result) before per-user identity was implemented.

Direct-publish avoids this by making each user complete a one-time Foundry
sign-in (via a Microsoft-managed OAuth broker) before their first tool call.
`app.py` replicates the same effect using the M365 Agents SDK's built-in OAuth
+ on-behalf-of (OBO) support. **Per-user identity is mandatory and fail-closed**:

- `on_message` is registered with `auth_handlers=["FOUNDRY"]`, so the SDK
  handles the sign-in (silent SSO via token exchange; card as fallback) + OBO
  token exchange before the handler runs.
- The exchanged user token builds a **per-request** OpenAI client
  (`_user_client`); that same client is used for the agent call, the file
  downloads, and orphan-file recovery, so everything in one turn is
  consistently scoped to that user.
- The conversation map is keyed by **user** (Entra object id), not by Teams
  thread — matching direct-publish's per-identity session.
- **No shared fallback**: if no user token can be obtained, the turn is
  refused with a sign-in message. If the OAuth settings are missing entirely,
  the app refuses to start. (The shared/key client exists only for the offline
  test harness and never serves live traffic.)
- `AgentApplication` must be constructed with `connection_manager=` (see
  `app.py`) — without it the SDK's OBO handler crashes with
  `'NoneType' object has no attribute 'get_connection'`.

**Setup (live-verified 2026-07-16/17):**

1. **App registration** (bot identity): Application ID URI
   `api://botid-<bot-app-id>` exposing scope `access_as_user`; pre-authorize
   the Teams clients; redirect URI
   `https://token.botframework.com/.auth/web/redirect`.
2. **Bot OAuth connection** (`Aadv2` provider, e.g. named `TeamsSSO`):
   - Scopes: `api://botid-<bot-app-id>/access_as_user`
   - Token Exchange URL: `api://botid-<bot-app-id>` (enables silent SSO)

   ```powershell
   az bot authsetting create -n <bot-name> -g <rg> -c TeamsSSO --client-id <bot-app-id> --client-secret <bot-app-secret> --service Aadv2 --provider-scope-string "api://botid-<bot-app-id>/access_as_user"
   ```
3. **Teams manifest**: `webApplicationInfo.id` = bot app id;
   `webApplicationInfo.resource` = `api://botid-<bot-app-id>` (**no scope
   suffix** — it must equal the Application ID URI exactly; a `/access_as_user`
   suffix breaks SSO with "app does not exist or has been uninstalled");
   `validDomains` must include `token.botframework.com` and
   `login.microsoftonline.com`.
4. **App settings** (see `.env.example`): `AZUREBOTOAUTHCONNECTIONNAME` = the
   Bot OAuth connection name; `OBOCONNECTIONNAME=SERVICE_CONNECTION` (the MSAL
   connection performs the exchange — not the Bot OAuth connection);
   `SCOPES=https://ai.azure.com/.default` (Foundry rejects other audiences
   with 401 "audience is incorrect").
5. **Per end user**: Foundry RBAC role (Agent Consumer / Foundry User), Fabric
   data agent + data-source access, Fabric "Copilot and Azure OpenAI" tenant
   settings coverage — see "Security model".

**Status: live-verified through real Teams (2026-07-16/17)** — sign-in, OBO,
and per-user Foundry calls all working. Remaining validation: the two-user
Fabric test (different table access → different results) once Fabric tenant
settings (cross-geo Azure OpenAI processing for non-EU/US capacities) and
per-user roles are in place for the testers.

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
2. Live traffic runs under each end user's own token (per-user OBO), so the
   box identity is NOT used for agent calls. Grant end users **Foundry Agent
   Consumer** (agent scope) or **Foundry User** (project scope; formerly named
   Azure AI User — note Microsoft's RBAC doc says do NOT use "Azure AI
   Developer" for Foundry work). Role assignment requires Owner/User Access
   Administrator; a Contributor cannot assign roles. `FOUNDRY_API_KEY` /
   managed identity only serve the offline pipeline test.
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
