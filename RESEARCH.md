# Why the built-in "Publish to Teams" can't deliver files & charts

Research findings, verified 2026-07-13 against official Microsoft documentation
and live inspection of our Azure environment. This is the evidence base for
building a custom relay instead of using (or waiting on) the built-in path.

## TL;DR

Azure AI Foundry's "Publish to Teams" wires Teams to a **Microsoft-hosted,
closed endpoint** that forwards only text. Code Interpreter outputs are
returned as `file_id` references that must be exchanged for bytes via an
authenticated API call — the hosted endpoint never does this, so files surface
as dead `sandbox:/mnt/data/...` links and charts never appear. No no-code fix
exists as of July 2026. The supported customization point is replacing the
bot's messaging endpoint with your own relay — which is what this repo does.

## 1. The mechanism of the failure

Code Interpreter does not produce URLs. A response carries:

- text containing `sandbox:/mnt/data/<name>` pseudo-links (meaningless outside
  the sandbox), and
- `container_file_citation` annotations: `{file_id, container_id, filename}`.

Rendering an output requires an **authenticated** call:
`GET {project}/openai/v1/containers/{container_id}/files/{file_id}/content`
(token scoped to `https://ai.azure.com`). The Foundry playground does this —
that's why files/charts work there. Teams is a generic Bot Framework client:
it renders `activity.text` and `activity.attachments` and can't call the
Files/Containers API. Whatever sits between Teams and the agent must do the
exchange. The built-in endpoint doesn't.

Extra constraint: Code Interpreter containers expire (~1 h active / 30 min
idle), so the exchange must happen at response time — it can't be deferred to
click time.

## 2. What the wizard actually deploys (verified in our tenant)

Inspecting the wizard-created resources in `rg-johnbaby-6109_ai` showed the
bots' messaging endpoints point at a **Foundry-hosted** URL:

```
https://<resource>.services.ai.azure.com/api/projects/<project>/agents/
    <agent>/endpoint/protocols/activityprotocol?api-version=2025-11-15-preview
```

There is no App Service / Function App with relay code to modify — the relay
is inside Microsoft's service, a black box. The only configurable seam Bot
Framework offers is the messaging endpoint URL on the Azure Bot resource.

Also verified: the wizard bot's identity (`msaAppId`) is the agent's own
managed identity — Entra `servicePrincipalType: ServiceIdentity`, display name
`...-<agent>-AgentIdentity`, `appOwnerOrganizationId: null`, created from a
`ManagedAgentIdentityBlueprint`. **No application object exists**, so no
client secret can ever be issued for it; and a bot's `msaAppId` is immutable.
Consequence: a custom relay cannot authenticate as the wizard's bot — it needs
its own app registration, bot resource, and (one-time admin-approved) Teams
app.

## 3. Official confirmation that the gap is open (July 2026)

- **Publish-flow limitations table** (learn.microsoft.com, updated 2026-07-10):
  file uploads and image generation don't work in M365 Copilot; published
  agents don't support streaming or citations. Nothing delivers generated
  files/charts as Teams attachments.
  https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network
- **Open community thread** (Feb 12 – Jul 1, 2026, no Microsoft fix):
  "Foundry Agent deployed to Copilot/Teams Can't Display Images Generated via
  Code Interpreter" — confirms dead sandbox links in Teams.
  https://techcommunity.microsoft.com/discussions/azure-ai-foundry-discussions/-/4494610
- **Copilot Studio is not an escape hatch**: its own code-interpreter FAQ
  (Feb 2026) states "Images created with code interpreter are not rendered in
  the Teams and Microsoft 365 Copilot channel."
  https://learn.microsoft.com/en-us/microsoft-copilot-studio/faq-code-interpreter
- **M365 Agents SDK** is bot plumbing only (adapter/hosting); it has no
  automatic handling of Foundry code-interpreter outputs.

So the fix is not "configure a feature" — the feature doesn't exist. The
custom relay (download by `file_id`, re-deliver as Teams attachments) is the
standard production pattern, and both of its interfaces (Bot Framework
activities in, Responses API out) are public and supported.

## 4. SDK landscape that shaped the implementation

- **Classic Agents API (threads/runs, `asst_*` ids) is retiring**: the
  underlying Assistants infrastructure retires **2026-08-26**; Foundry classic
  agents 2027-03-31, but Microsoft's own guidance says don't rely on the later
  date. New Foundry portal agents (ours included — `kind: prompt`) already
  speak the Responses/conversations surface. → `app.py` targets
  `azure-ai-projects 2.3.0` + `project.get_openai_client()`;
  `app_classic.py` exists only as a fallback.
  https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/migrate
- **Bot Framework SDK (`botbuilder-*`) is retired** (final LTS ended
  2025-12-31). → `app.py` uses the GA replacement, the M365 Agents SDK
  (`microsoft-agents-*` 1.1.0).
  https://github.com/microsoft/botframework-sdk
- **Teams bot message size limit is ~100 KB** (not the old 28 KB, which now
  applies only to webhooks/connectors), and base64 card images are excluded
  from the count. Data-URI card images remain formally undocumented, so the
  relay falls back to blob+SAS above `DATA_URI_MAX_BYTES`.
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/format-your-bot-messages

## 5. Architecture: before vs after

```
BEFORE (wizard)                          AFTER (this repo)
Teams                                    Teams
  │                                        │
Azure Bot ──► Foundry-hosted endpoint    Azure Bot (new) ──► OUR RELAY (app.py)
              (activityprotocol,           │ per-chat Foundry conversation
               closed, text-only)          │ responses.create(agent_reference)
  │                                        │ downloads outputs at response time
  ▼                                        ├─ image → Adaptive Card (inline/SAS)
text ✔  charts ✘  files ✘ (dead links)     ├─ file  → Blob + SAS Download button
                                           └─ strips sandbox links, dedupes
                                         text ✔  charts ✔  files ✔
```

The agent, project, model, and Teams app model are unchanged — only the
middle box is replaced, using supported interfaces on both sides.

## 6. Live verification log (2026-07-13)

- `responses.create` with `extra_body={"agent_reference": ...}` → 200,
  completed, against `test-visualization-agent`.
- `containers.files.content.retrieve` → CSV and PNG bytes downloaded.
- Blob upload + SAS URL → fetched over plain HTTPS, HTTP 200.
- Full bot layer (anonymous auth + mock channel): typing indicators + message
  activity with correct Adaptive Cards delivered end to end.
- Observed model nondeterminism: occasionally a file is written but no
  citation annotation is emitted → relay recovers by listing the container
  (`container_id` from the `code_interpreter_call` item) and matching the
  sandbox filename.

## 7. Standing risks for a customer offering

- Microsoft may close this gap natively; the relay then becomes optional
  (revert = point the bot endpoint back). Track the publish-flow limitations
  page.
- Parts of the Foundry agent surface are preview-labeled; pin SDK versions
  (see `requirements.txt`) and budget for maintenance as APIs churn.
