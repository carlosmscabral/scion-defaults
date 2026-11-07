# Lessons learned

What went wrong while building and running the `psearch` / `psearch-eval` factories (October 2026), why, and where the
fix now lives. Dates are UTC. "Encoded in" points to the script, template rule or runbook that prevents a repeat.

| # | Lesson | Area |
|---|---|---|
| [L1](#l1-nested-ssh-quoting-runs-your-text-as-commands) | Nested SSH quoting runs your text as commands | operations |
| [L2](#l2-the-web-terminal-button-stopped-working) | The web terminal button stopped working | Hub |
| [L3](#l3-opencode-picked-models-that-dont-exist) | OpenCode picked models that don't exist | harness |
| [L4](#l4-a-printed-verdict-deadlocked-the-factory-for-about-60-hours) | A printed verdict deadlocked the factory for ~60 hours | protocol |
| [L5](#l5-leads-looked-stalled-and-nothing-woke-them) | Leads looked stalled and nothing woke them | protocol |
| [L6](#l6-antigravity-couldnt-run-any-tool-telemetry-hook-hang--and-its-phase-j-resolution) | Antigravity couldn't run any tool (telemetry hook hang) — resolved in Phase J | harness |
| [L7](#l7-the-default-compute-service-account-is-not-a-runtime-identity) | The default compute SA is not a runtime identity | GCP |
| [L8](#l8-workspaces-shared-folder-today-worktrees-later) | Workspaces: shared folder today, worktrees later | Scion |
| [L9](#l9-agents-fabricated-telemetry-and-claimed-storage-that-didnt-exist) | Agents fabricated telemetry and claimed storage that didn't exist | protocol |
| [L10](#l10-admin-api-access-from-scripts) | Admin API access from scripts | Hub |
| [L11](#l11-schedules-heartbeats-semantics) | Schedules (heartbeats) semantics | Hub |
| [L12](#l12-spanner-calls-from-the-vm-failed-on-quota-project) | Spanner calls from the VM failed on quota project | GCP |
| [L13](#l13-interactive-gcloud-prompts-hang-automation) | Interactive gcloud prompts hang automation | GCP |
| [L14](#l14-a-shared-github-token-means-no-pr-approvals) | A shared GitHub token means no PR approvals | GitHub |
| [L15](#l15-the-lead-wrote-the-code-itself) | The lead wrote the code itself | protocol |
| [L16](#l16-hand-written-hub-db-rows-broke-templates) | Hand-written Hub DB rows broke templates | Hub |
| [L17](#l17-pre-provision-the-identities-the-backlog-will-need) | Pre-provision the identities the backlog will need | process |
| [L18](#l18-hub-activity-is-a-hint-not-the-truth) | Hub activity is a hint, not the truth | observability |
| [L19](#l19-heartbeats-can-turn-into-chatter) | Heartbeats can turn into chatter | protocol |
| [L20](#l20-local-patches-dont-survive-upgrades-silently) | Local patches don't survive upgrades silently | operations |
| [L21](#l21-reincarnate-is-refused-for-agents-of-these-projects) | Reincarnate is refused for agents of these projects | Scion |
| [L22](#l22-hub-secret-injection-as_needed-vs-always) | Hub secret injection: `as_needed` vs `always` | security |
| [L23](#l23-deployer-workspace-cleanliness-and-cloud-run-traffic-routing) | Deployer workspace cleanliness and Cloud Run traffic routing | operations |
| [L24](#l24-unannounced-hub-restarts-overwrite-managed-harness-files-update_policy-notify) | Unannounced Hub restarts overwrite managed harness files | Hub |
| [L25](#l25-initialize-eval_branch-as-an-orphan-branch-git-switch---orphan-so-it-can-never-be-merged-into-main) | Initialize `EVAL_BRANCH` as an orphan branch | git |
| [L26](#l26-multi-harness-upgrade-to-gemini-4-argon-antigravity-opencode-hermes) | Multi-harness upgrade to Gemini 4 Argon (Antigravity, OpenCode, Hermes) | harness |
| [L27](#l27-cross-process-trace-state-tracestatego-and-stacked-parent-child-conversation-traces-in-cloud-trace) | Cross-process trace state (`tracestate.go`) and stacked Cloud Trace waterfalls | observability |
| [L28](#l28-several-factories-in-one-monorepo-names-start-folders-and-what-the-e2e-showed) | Several factories in one monorepo: names, start folders, and shared dirs | Scion |
| [L29](#l29-a-real-monorepo-kubernetes-as-three-factories-and-leads-that-start-their-own-teams) | A real monorepo (Kubernetes) as three factories, and leads that start their own teams | protocol |
| [L30](#l30-multi-app-repositories-the-13-agent-specialist-fabric-deploy-none-and-autonomous-self-healing) | Multi-app repositories, the 13-agent specialist fabric (`deploy: none`), and self-healing | fabric |
| [L31](#l31-20-hour-autonomous-30-issue-sweep-shared-worktree-commit-slots-token-scope-graceful-degradation-iac-specialists-and-eval-sweep-guarding) | 20-hour autonomous 30-issue sweep: shared-worktree commit slots, token-scope graceful degradation, IaC specialists, and `eval-sweep` guarding | autonomy |

---

## L1. Nested SSH quoting runs your text as commands

- **When:** throughout; worst on 2026-10-12 while sending messages to agents.
- **Symptom:** `zsh: command not found: lead`, truncated messages, half-written files.
- **Cause:** `gcloud compute ssh --command "... scion message lead \"... `lead` ...\""` passes through two or three shells.
  Backticks and `$(...)` inside the text run as commands; nested quotes end early.
- **Fix:** ship content base64-encoded and decode it remotely (`echo <b64> | base64 -d | sudo -u scion bash`).
- **Encoded in:** [`scripts/run-on-hub.sh`](../scripts/run-on-hub.sh). Agents use `scion message --body-file` for long text
  ([`_shared/agents.md`](../templates/_shared/agents.md)).

## L2. The web terminal button stopped working

- **When:** 2026-10-09.
- **Symptom:** the agent's Terminal button in the web UI did nothing or showed a blank terminal.
- **Cause:** the `web.terminal_workspace` experiment (beta, on by default) holds a cross-tab `navigator.locks` lock,
  which a hidden tab can keep forever, and `initTerminal` waits on `requestAnimationFrame`, which background tabs
  throttle.
- **Fix:** turn the experiment off: `scripts/run-on-hub.sh scripts/scion-admin.py experiments --set
  web.terminal_workspace=false`; users reload the page.
- **Encoded in:** [operations-runbook.md](operations-runbook.md#web-terminal-button-does-nothing).

## L3. OpenCode picked models that don't exist

- **When:** 2026-10-08/09 (`backend`).
- **Symptom:** every model call returned 404; OpenCode switched to image or text-to-speech variants.
- **Cause:** OpenCode resolves models from the models.dev catalog in `~/.cache/opencode/models.json`. That catalog marks
  Gemini 2.5 as deprecated and lists several "Gemini 2.5 Pro" variants; the name matched a TTS variant first. The Scion
  provisioner pre-fetches the full catalog at every start, the stock launcher then deleted it, and OpenCode refreshes it
  in the background (the live `backend` had a fresh 5.4 MB catalog again on 2026-10-12, three days after the pin).
- **Fix:** the launcher writes a pinned catalog with only `google-vertex/gemini-2.5-pro` and `gemini-2.5-flash` on every
  start, then runs `opencode -m google-vertex/gemini-2.5-pro`. To reload a running agent without recreating it:
  `docker exec -u scion <project>--backend tmux respawn-pane -k -t scion:0.0 "sh /home/scion/.local/bin/opencode-launch.sh"`.
- **Encoded in:** [`templates/backend/home/.local/bin/opencode-launch.sh`](../templates/backend/home/.local/bin/opencode-launch.sh),
  [`config/opencode-models.json`](../config/opencode-models.json).

## L4. A printed verdict deadlocked the factory for about 60 hours

- **When:** 2026-10-10 00:05Z to 2026-10-12 12:03Z.
- **Symptom:** no commits or comments for about 60 hours; every agent idle.
- **Cause:** the lead told the deployer to hold issue #11 "until my GO" and waited for the reviewer. The reviewer
  (Hermes) wrote `lead <- reviewer: ... FAIL` in its own output but never ran `scion message`, so nobody received it.
  Lead, deployer and reviewer then waited on each other, and nothing woke them up. The FAIL was also wrong: the reviewer
  looked for guessed file names (`ADR-001.md`, `011-gemini-v2-embeddings.sql`) instead of the real
  `knowledge-plane/adr/ADR-001-hybrid-and-vector-1024.md` and `src/iac/migrations/011_add_embedding_v2_gemini_1024.sql`.
- **Fix:** the PO sent the lead the verdict with the correct paths; the reviewer re-checked and delivered PASS through
  `scion message`; heartbeats were added (L5).
- **Encoded in:** "check for `Message delivered`" and "use real paths" in [`_shared/agents.md`](../templates/_shared/agents.md);
  the reviewer's FAIL must cite a file and line or a failing command ([`reviewer`](../templates/reviewer/system-prompt.md));
  GitHub as the record of every handoff.

## L5. Leads looked stalled and nothing woke them

- **When:** found 2026-10-10, fixed 2026-10-12.
- **Symptom:** 8 of 10 agents showed `stalled` in the Hub; leads stopped after "waiting for backend..." and never came
  back on their own.
- **Cause:**
  - A Claude agent that ends its turn without `sciontool status blocked` is flagged STALLED after about 5 minutes. The
    first templates said "Do NOT run `sciontool status`". Scion's own workstation default template (`agents.md`) tells agents to
    use `sciontool status`; in hosted mode, the bundled default template's `agents.md` is empty (0 bytes), so our custom templates must supply this guidance.
  - Agents only react to input. No schedule existed, so a lost message meant a permanent wait.
  - Hub notifications about an agent go to its creator. The admin created all agents, so worker notifications went to
    the PO, not the leads. Repeated identical notifications are deduplicated.
- **Fix:** admin-owned heartbeat schedules (`*/15 * * * *`) that message each lead, and the rule to end every turn
  with a status. Since then the heartbeats have fired every 15 minutes without errors.
- **Encoded in:** [`config/heartbeat-message.txt`](../config/heartbeat-message.txt),
  [setup-runbook.md](setup-runbook.md#9-heartbeats), "End every turn with a status" in
  [`_shared/agents.md`](../templates/_shared/agents.md).

## L6. Antigravity couldn't run any tool (telemetry hook hang) — and its Phase J resolution

- **When:** 2026-10-12 12:17Z (initial workaround 12:31Z, durable workaround 12:38Z; definitively resolved in Phase J on 2026-10-14).
- **Symptom:** every tool call of the Antigravity agents failed; the eval team produced nothing.
- **Initial Diagnosis & Temporary Workaround:** with Hub telemetry and cloud export on, `sciontool hook --dialect=antigravity` printed `{"decision":"allow"}`
  but did not exit (it waited on cloud export to `monitoring.googleapis.com`). Antigravity kills hooks after 10 seconds and
  aborts the tool call. As a temporary workaround, telemetry was turned off in the hook command:
  `... | SCION_TELEMETRY_ENABLED=false sciontool hook --dialect=antigravity`.
- **Root Cause (diagnosed in Phase J):** The container entrypoint (`sciontool init`, PID 1) was failing to bind the local OpenTelemetry
  receiver on ports `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP) because `SCION_GCP_PROJECT_ID` (and GCP credentials) were missing
  from the container runtime environment. Because no background OTLP collector was listening inside the container, short-lived `sciontool hook`
  processes had nowhere to flush locally, attempting synchronous cloud export directly on each hook invocation and hanging when network or credentials were unconfigured.
- **Definitive Fix (Phase J End-to-End Telemetry):**
  1. **Telemetry Infrastructure & Credentials:** [`scripts/setup-telemetry.sh`](../scripts/setup-telemetry.sh) enabled GCP Cloud Logging, Cloud Monitoring,
     and Cloud Trace APIs, provisioned the `scion-telemetry-writer` service account with required IAM roles, and deployed credentials to `~/.scion/telemetry-gcp-credentials.json`
     on the Hub VM and container homes.
  2. **Sciontool Wrapper & `ETXTBSY` Prevention:** [`scripts/patch-sciontool-telemetry.sh`](../scripts/patch-sciontool-telemetry.sh) installs the patched
     binary at `/usr/local/bin/sciontool.real` and unlinks `/usr/local/bin/sciontool` (`rm -f /usr/local/bin/sciontool` to avoid Linux `ETXTBSY` while PID 1
     is running) before installing the executable wrapper `/usr/local/bin/sciontool` across all 10 running containers and base container images
     (`scion-antigravity`, `scion-claude`, `scion-opencode`, `scion-hermes`). The wrapper exports `SCION_GCP_PROJECT_ID`, `SCION_OTEL_GCP_CREDENTIALS`,
     `SCION_TELEMETRY_ENABLED=true`, `SCION_TELEMETRY_CLOUD_PROVIDER=gcp`, and per-harness `SCION_USAGE_SOURCE` / `SCION_MODEL` (`native` + `opus` for
     `claude`; `hooks` + `Gemini 4 Argon (High)` for `antigravity`; `hooks` + `google-vertex/gemini-4-argon` for `opencode`; `hooks` + `google/gemini-4-argon`
     for `hermes`) before executing `sciontool.real`.
  3. **Fix for `collectorEpoch=0` in `flushMetricBuffer` (`pkg/sciontool/telemetry/pipeline.go:915`):** In `sciontool` `20261002`, `metricFlushLoop` ticks 1s
     after startup and calls `flushMetricBuffer`, which initialized `p.metricStreams = newMetricStreams()` with `gcp: false` before any metric arrived via
     `handleMetrics`. Subsequent hook/usage counters inherited `collectorEpoch = 0` (`StartTimeUnixNano: 0`), which Cloud Monitoring rejected with
     `InvalidArgument: Field points[0].interval.start_time had an invalid value ... The start time must be positive`. Setting `p.metricStreams.gcp = p.config.IsGCP()`
     and `p.metricStreams.now = p.now` in both `flushMetricBuffer` and `handleMetrics` resolves this completely.
  4. **Verified Listening Ports & Clean Hook Execution:** On restart, PID 1 initializes the GCP-native cloud exporter and starts the telemetry
     pipeline listening on `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP). Short-lived `sciontool hook` invocations flush immediately to loopback and
     exit in <0.3s.
  5. **Clean Hooks Restored:** [`scripts/patch-antigravity-hooks.py`](../scripts/patch-antigravity-hooks.py) and [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py)
     restore clean `sciontool hook --dialect=antigravity` calls in all `provision.py`, `.agents/hooks.json`, pre-start scripts, and Hub DB project hooks,
     with telemetry enabled and Gemini 4 Argon preserved.
  6. **Verified End-to-End Flow:** All 10 agent containers report `scion.telemetry.pipeline.status=1`, `gen_ai.api.calls`, `scion.usage.tokens`, and
     `agent.tool.calls` in Google Cloud Monitoring (`Failed:0`), container and server logs flow to `projects/<id>/logs/scion-agents` and
     `projects/<id>/logs/scion-server` in Cloud Logging, traces flow to Cloud Trace, the Hub metrics dashboard API (`GET /api/v1/metrics/dashboard`) serves
     live multi-harness session/call/token metrics at HTTP 200, and [`scripts/sync-factory-agents.py`](../scripts/sync-factory-agents.py) syncs live Cloud Monitoring
     call/tool counts per `scion_agent_id` into Cloud Spanner `factory_agent_status`.
- **Encoded in:** [`scripts/setup-telemetry.sh`](../scripts/setup-telemetry.sh), [`scripts/patch-sciontool-telemetry.sh`](../scripts/patch-sciontool-telemetry.sh),
  [`scripts/patch-antigravity-hooks.py`](../scripts/patch-antigravity-hooks.py), and [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py).

## L7. The default compute service account is not a runtime identity

- **When:** 2026-10-12 12:30Z (rolled back), fixed 22:46Z.
- **Symptom:** switching `psearch-serving` to Gemini embeddings returned 403 from Vertex AI; the deployer rolled back.
- **Cause:** Cloud Run ran as the project's default compute service account, which lacked `roles/aiplatform.user`.
- **Fix:** a dedicated runtime SA with exactly what the code uses (`roles/aiplatform.user` on the project,
  `roles/spanner.databaseUser` on the database); the deployer has `roles/iam.serviceAccountUser` on it.
- **Also learned:** `gemini-embedding-2` is served at location `global` through `:embedContent` (`:predict` returns 404),
  with `outputDimensionality: 1024`. Spanner full-text search doesn't fold accents: "tenis" doesn't match "Tênis"
  (relevant to the multilanguage issue).
- **Encoded in:** [`scripts/setup-sandbox.sh`](../scripts/setup-sandbox.sh) creates the runtime SA up front; the deployer
  deploys with `--service-account` ([`deployer`](../templates/deployer/system-prompt.md)).

## L8. Workspaces: shared folder today, worktrees later

- **When:** researched 2026-10-10.
- **What we found:**
  - A Hub-managed project without a git remote mounts one shared folder (`~/.scion/projects/<slug>`) as `/workspace`
    in every agent. Parallel issues are impossible and commits must be serialized (the "commit slot").
  - A project's `git_remote` can't be changed later: switching to a git-backed mode means creating new projects.
  - Clone-per-agent works for git-backed projects today. Worktree-per-agent has gaps on a VM broker: it needs host git
    2.47 or newer (the VM had 2.34.1 and silently fell back), the repository's `.git` is not mounted for
    broker-provisioned worktrees, there are no push credentials or git identity in the container, the base clone is
    never fetched, and deleting an agent leaks its worktree (created by UUID, deleted by slug).
- **Decision:** keep the shared workspace with a commit slot for now; move to clone-per-agent with one branch and PR
  per issue in v2; fix worktree-per-agent in Scion as a separate project.
- **Encoded in:** "Git in the shared workspace" in [`_shared/agents.md`](../templates/_shared/agents.md);
  [v2 design](specs/factory-orchestration-v2-design.md).

## L9. Agents fabricated telemetry and claimed storage that didn't exist

- **When:** found 2026-10-12 22:46Z, fixed by 23:25Z.
- **Symptom:** the eval dashboard showed plausible agent counters and a "GitHub repository & branch telemetry" panel that
  stayed empty.
- **Cause:** the eval agents hard-coded counters in `ingest_telemetry.py` and `public/telemetry.json`, reported
  Firestore and bucket storage that had never been created, and the dashboard called `/api/backlog`, a route that
  didn't exist (the server returned `index.html`). Nobody checked the claims against the source.
- **Fix:** real sources only: a Hub-to-Spanner sync of agent state every minute; the GitHub API with the token from
  Secret Manager; probe results only from real runs in `eval_runs`; counters the Hub doesn't report shown as "n/a".
  Firestore and the bucket were then created for real.
- **Encoded in:** "Never invent numbers" in [`_shared/agents.md`](../templates/_shared/agents.md); the data-source table in
  [`eval-lead`](../templates/eval-lead/system-prompt.md); [`scripts/sync-factory-agents.py`](../scripts/sync-factory-agents.py).
  Verify any delivery with `curl` against the live service before you accept it.

## L10. Admin API access from scripts

- **What we found:**
  - Personal access tokens (`scion_pat_...`) are rejected on hub-admin routes (`/api/v1/admin/*`) and can't author
    schedules.
  - An admin user JWT can be minted on the VM from `SCION_SERVER_SESSION_SECRET` in `~/.scion/hub.env`; it is
    equivalent to an admin web session, so keep it short-lived and on the VM.
  - `scion list` on the VM fails outside a project context, and the project settings point at the public (IAP) URL.
    For reads, use the Hub DB read-only (`sqlite3 'file:...hub.db?mode=ro'`) or the localhost API.
  - Docker on the VM belongs to the Hub OS user (`sudo -u scion docker ...`). Read an agent's terminal with
    `docker exec -u scion <project>--<agent> tmux capture-pane -p -t scion:0.0 -S -50`.
- **Encoded in:** [`scripts/scion-admin.py`](../scripts/scion-admin.py), [`scripts/factory-status.sh`](../scripts/factory-status.sh).

## L11. Schedules (heartbeats) semantics

- Each fire is re-authorized against the schedule's creator. A schedule created by an agent stops working if that agent
  is deleted, so the admin creates them.
- Any agent belonging to a project has permission to pause, resume, or edit that project's schedules (M1).
  If heartbeats stall unexpectedly, check that a confused worker has not disabled or re-pointed the schedule.
- Cron expressions are in UTC; the minimum useful interval is about a minute.
- To change a schedule's message, PATCH its `payload` (a JSON string with `agentName` and `message`).
- A schedule can report a successful fire while the message didn't reach the harness, and scheduled messages and
  notifications are not stored as message rows. Judge a heartbeat by its effect (the lead acts, GitHub moves), not by
  the run count alone.
- **Encoded in:** `schedule` subcommands of [`scripts/scion-admin.py`](../scripts/scion-admin.py);
  [operations-runbook.md](operations-runbook.md#a-lead-doesnt-react-to-heartbeats).

## L12. Spanner calls from the VM failed on quota project

- **When:** 2026-10-12 22:55Z.
- **Symptom:** `gcloud spanner databases execute-sql` from the Hub VM was denied although the VM SA had database access.
- **Cause:** the VM SA's quota project defaults to the Hub project, where the call isn't allowed.
- **Fix:** `--billing-project=<sandbox>` on every call, plus `roles/serviceusage.serviceUsageConsumer` for the VM SA on
  the sandbox.
- **Encoded in:** [`scripts/sync-factory-agents.py`](../scripts/sync-factory-agents.py) (`BILLING_PROJECT`),
  [`scripts/setup-sandbox.sh`](../scripts/setup-sandbox.sh).

## L13. Interactive gcloud prompts hang automation

- **When:** first on 2026-10-09, and again on 2026-10-13 while testing `setup-sandbox.sh` in dry-run mode against a
  project that doesn't exist: `gcloud firestore databases describe` waited for an answer to "API not enabled, enable
  and retry?" forever.
- **Fix:** enable APIs explicitly before using them, and set `CLOUDSDK_CORE_DISABLE_PROMPTS=1` in every script and in
  the deploy agents' environment.
- **Encoded in:** [`scripts/setup-sandbox.sh`](../scripts/setup-sandbox.sh), the deployer templates' `env`.

## L14. A shared GitHub token means no PR approvals

- **What we found:** all agents push and comment with the PO's personal access token, so GitHub sees the PO as the
  author of every PR and doesn't let the "reviewer" approve or request changes on its own PR.
- **Decision:** the reviewer posts a signed verdict comment; no branch protection requires approvals. A GitHub App
  (bot identity) is the long-term fix.
- **Encoded in:** [v2 design](specs/factory-orchestration-v2-design.md), "Pull requests".

## L15. The lead wrote the code itself

- **When:** 2026-10-09/10 (issue #11).
- **Symptom:** after two failed backend attempts, the lead implemented #11 itself; nobody orchestrated meanwhile.
- **Fix:** the lead never writes product code; it re-briefs the worker with what went wrong, or escalates.
- **Encoded in:** [`lead`](../templates/lead/system-prompt.md).

## L16. Hand-written Hub DB rows broke templates

- **When:** found 2026-10-12.
- **Symptom:** the five eval templates (`eval-lead`, `eval-deployer`, `factory-analyst`, `search-evaluator`,
  `ui-craftsman`) all point at the `frontend` template's storage path in the Hub DB.
- **Cause:** the templates were registered by inserting rows straight into the Hub's SQLite DB with a copied storage
  path, so the eval agents were actually provisioned from the frontend template's files.
- **Trap:** don't fix it with `scion templates push`. A push to an existing template uploads into that row's storage
  path, which here belongs to `frontend`: it would overwrite the frontend template's files. Deleting the rows with
  their files (`deleteFiles=true`) would delete the frontend's files; deleting them without files leaves the running
  agents pointing at a template ID that no longer exists.
- **Fix (2026-10-13):** rename each broken row to `<role>-legacy` (a metadata PATCH; storage untouched, and running
  agents keep resolving it by ID), then create `<role>` again so it gets its own storage path. Result: five new rows
  with their own files, the legacy rows still resolving for the running agents, and the frontend template's content
  hash unchanged. A plain restart of an eval agent still uses its legacy row, as before; a recreated agent uses the
  new template (reincarnate is refused here, L21).
- **Encoded in:** [`scripts/push-templates.py`](../scripts/push-templates.py), which does this automatically and never
  uploads into a path a template doesn't own; [templates/README.md](../templates/README.md); scripts open the DB
  read-only and write only through the API ([scripts/README.md](../scripts/README.md)).

## L17. Pre-provision the identities the backlog will need

- **When:** 2026-10-12. The #11 rollout waited about 10 hours (12:30Z to 22:29Z) for an IAM decision; the lead
  correctly escalated and reminded the PO every 2 hours.
- **Lesson:** the PO's response time is the factory's slowest step. Decide the identities and grants the backlog needs
  during setup (runtime SA, data stores, secrets) so agents don't wait on them, and keep escalations to genuine
  decisions.
- **Encoded in:** [`scripts/setup-sandbox.sh`](../scripts/setup-sandbox.sh), [setup-runbook.md](setup-runbook.md).

## L18. Hub activity is a hint, not the truth

- **What we found:**
  - Activity is only as good as the harness's hooks. The Hermes reviewer shows `stalled` with its last activity three
    days old, although it reviewed and messaged the lead that morning: Hermes doesn't report activity events here.
  - Agents that finished a turn correctly still show `stalled` if they skipped `sciontool status` (L5).
  - The Hub doesn't populate turn and model-call counters (they stay 0); that's "unknown", not "zero".
- **Lesson:** decide liveness from evidence (GitHub comments and commits, delivered messages, the agent's terminal), and
  use Hub activity only as a pointer to where to look.
- **Encoded in:** [`scripts/factory-status.sh`](../scripts/factory-status.sh) shows all of these side by side;
  [operations-runbook.md](operations-runbook.md).

## L19. Heartbeats can turn into chatter

- **When:** 2026-10-12 23:45Z.
- **Symptom:** on each heartbeat the eval lead sent `HEARTBEAT_PING` to all three specialists, and each answered with a
  `PONG`/`ACK`: six messages and four model turns every 15 minutes with no work behind them.
- **Fix:** the heartbeat says to ping only agents you are waiting on, and to do nothing but set the status when nothing
  needs action.
- **Encoded in:** [`config/heartbeat-message.txt`](../config/heartbeat-message.txt), "Messaging" in
  [`_shared/agents.md`](../templates/_shared/agents.md).

## L20. Local patches don't survive upgrades silently

- **What we found:** In hosted Hub mode, the broker provisions agents from `~/.scion/workspace-storage/hubs/<hub-id>/harness-configs/global/<harness>/provision.py`
  rather than bare `~/.scion/harness-configs/`. Additionally, each agent container maintains its own local copy under
  `<agent-home>/.scion/harness/provision.py` which executes `20-harness-provision` on container startup. Simply patching
  bare `~/.scion/harness-configs/` does not affect hosted agent provisioning. Furthermore, automatic nightly channel updates
  or container rebuilds can wipe local harness file edits.
- **Lesson & Solution:** A multi-tier durable hook architecture is required:
  1. Patch both the Hub's workspace-storage global provisioner and existing agent-home provisioners.
  2. Install a container pre-start hook script (`30-project-custom`) in `<agent-home>/.scion/hooks/pre-start.d/` (mode 0700)
     to re-apply the hook patch inside `/workspace/.agents/hooks.json` on container start.
  3. Register and activate the `agy-telemetry-off` hook in the Hub SQLite database (`project_pre_start_hooks` table)
     for all active projects, ensuring new agent containers automatically inherit the hook.
- **Encoded in:** [`scripts/patch-antigravity-hooks.py`](../scripts/patch-antigravity-hooks.py) (supports check mode, `--apply`,
  and checks both filesystem and Hub DB), [operations-runbook.md](operations-runbook.md#after-a-scion-upgrade).

## L21. Reincarnate is refused for agents of these projects

- **When:** 2026-10-13, preparing to move `eval-lead` from Antigravity to Claude Code.
- **Symptom:** `POST /api/v1/projects/<id>/agents/<agent-id>/reincarnate` with `{"dryRun": true}` returns HTTP 400
  "reincarnate requires a clone-per-agent, shared-workspace or hub-managed workspace" for the agents of both
  Hub-managed projects (no git remote, broker on the Hub VM).
- **Cause (from the code):** the eligibility check in `pkg/hub/handlers_agent_reincarnate.go` uses the agent's
  "effective" workspace, and for a project linked on the broker the agent's absolute workspace path
  (`~/.scion/projects/<slug>`) is cleared first, so the agent looks like it has no workspace at all. Probably a Scion
  bug for this topology; worth reporting upstream.
- **Workaround:** recreate the agent instead: put the state on GitHub and write a short handoff, `scion delete
  <agent>`, then `scion start <agent> -t <template> "<handoff>"`. The new agent has a new ID: schedules address agents
  by name and keep working, but notification subscriptions and message history don't carry over.
- **Encoded in:** [operations-runbook.md](operations-runbook.md#switch-an-agents-harness).

## L22. Hub secret injection: `as_needed` vs `always`

- **What we found:** When secrets are stored in the Hub with `injection_mode: as_needed` (the default for some CLI paths),
  they are only injected into agent containers whose template explicitly lists that secret name in its `secrets` stanza.
  If an agent's template omits `GITHUB_TOKEN`, git pushes, issue comments, and PR management fail with authentication errors.
- **Decision:** Critical infrastructure secrets shared across all factory agents (like `GITHUB_TOKEN`) must have
  `injection_mode: always`. This guarantees every agent container receives the environment variable at boot time,
  preventing template misconfiguration from breaking agent operations.
- **Encoded in:** Hub database `secrets` table (`UPDATE secrets SET injection_mode='always' WHERE key='GITHUB_TOKEN'`),
  [setup-runbook.md](setup-runbook.md#5-github), [operations-runbook.md](operations-runbook.md#rotate-the-github-token).

## L23. Deployer workspace cleanliness and Cloud Run traffic routing

- **What we found:**
  - In a shared workspace, running `gcloud run deploy --source .` directly packages the entire workspace folder,
    including untracked files, scratch scripts, or concurrent edits from other agents.
  - When rolling back a Cloud Run service using `gcloud run services update-traffic --to-revisions <rev>=100`, traffic
    remains pinned to that revision. A subsequent `gcloud run deploy` builds and deploys a new revision, but Cloud Run
    continues routing 100% of traffic to the pinned rollback revision unless `--to-latest` is explicitly specified.
- **Fix:**
  - Deployers must export the target commit cleanly using `git archive "$REV" | tar -x -C "$TMP_DIR"` and deploy from `$TMP_DIR`.
  - The deployer system prompt specifies passing `--to-latest` on deployments (or running `gcloud run services update-traffic --to-latest`)
    to re-enable automatic traffic routing to newly deployed revisions after a rollback.
- **Encoded in:** [`templates/deployer/system-prompt.md`](../templates/deployer/system-prompt.md),
  [`templates/eval-deployer/system-prompt.md`](../templates/eval-deployer/system-prompt.md),
  [operations-runbook.md](operations-runbook.md#a-deploy-failed-or-needs-a-rollback).

## L24. Unannounced Hub restarts overwrite managed harness files (`update_policy: notify`)

- **What we found:** On startup, `scion server` executes `OverwriteBuiltinManaged`, which overwrites
  `workspace-storage/.../harness-configs/global/antigravity/provision.py` with default builtin templates.
  When `server.maintenance.update_policy` was set to `auto`, nightly Scion updates triggered unannounced service restarts,
  silently reverting the Antigravity telemetry hook patch and causing tool calls to time out (L6).
- **Fix:**
  1. Set `server.maintenance.update_policy: notify` in `~/.scion/settings.yaml`. Updates notify the operator without auto-restarting.
  2. The watchdog daemon ([`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py)) runs every 5 minutes and immediately
     detects and re-patches any provisioner drift if the service is restarted.
- **Encoded in:** [`config/hub-settings.example.yaml`](../config/hub-settings.example.yaml),
  [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py),
  [operations-runbook.md](operations-runbook.md#after-a-scion-upgrade).

## L25. Initialize `EVAL_BRANCH` as an orphan branch (`git switch --orphan`) so it can never be merged into `main`

- **What we found:** When two parallel factories (`psearch` building the product and `psearch-eval` observing it) share a single GitHub repository, branching `eval-dashboard` (`EVAL_BRANCH`) off `main` (`BUILD_BRANCH`) creates shared Git ancestry. This introduces two serious failure modes:
  1. The evaluation workspace inherits product application code, build files, and Dockerfiles (`cmd/`, `internal/`, `go.mod`, etc.) that are irrelevant to the eval dashboard and cause confusing git conflicts or mistaken test runs.
  2. A confused agent or automated tool can attempt to merge, rebase, or open a pull request between the branches, risking product regressions or polluting the product history with eval tooling.
- **Fix:**
  - Initialize `EVAL_BRANCH` as an **orphan branch** using `git switch --orphan "$EVAL_BRANCH"` (or create an empty root commit and push).
  - An orphan branch has 0 shared ancestor commits with `main` (`git merge-base origin/main origin/eval-dashboard` exits 1). Git and GitHub will refuse unrelated history merges by default.
  - In existing repositories where `eval-dashboard` was branched off `main`, rewrite the branch root or strip stale application files from `eval-dashboard` history using `git filter-branch` while temporarily pausing branch protection rulesets.
- **Encoded in:** [setup-runbook.md](setup-runbook.md#6-hub-projects-and-the-deployer-identity),
  [architecture.md](architecture.md#components),
  [agent-protocol.md](agent-protocol.md#git-in-a-shared-workspace),
  [`templates/_shared/agents.md`](../templates/_shared/agents.md),
  [`templates/lead/system-prompt.md`](../templates/lead/system-prompt.md),
  [`templates/eval-lead/system-prompt.md`](../templates/eval-lead/system-prompt.md).

## L26. Multi-harness upgrade to Gemini 4 Argon (Antigravity, OpenCode, Hermes)

- **When:** 2026-10-13 / 2026-10-14.
- **Goal:** Upgrade all factory worker harnesses (6 Antigravity agents, 1 OpenCode backend agent, 1 Hermes reviewer agent) from legacy Gemini 2.5 / 3.x to Gemini 4 Argon on Vertex AI while leaving Claude Code agents (lead, deployer) undisturbed.
- **Symptom & Root Causes across harnesses:**
  1. **Vertex AI Global Endpoint (`locations/global`):**
     Gemini 4 Argon on Vertex AI is served from the global publisher location (`https://aiplatform.googleapis.com/v1/projects/.../locations/global/publishers/google/models/gemini-4-argon`). Regional endpoints (e.g. `us-central1`) return `404 Not Found` or model not recognized. All harnesses require explicit global location overrides (`GOOGLE_CLOUD_LOCATION=global`, `VERTEX_LOCATION=global`, `VERTEX_REGION=global`).
  2. **Hermes Configuration Override Precedence:**
     Hermes loads `~/.hermes/.env` with `override=True` (in `env_loader.py`). Even if `~/.hermes/config.yaml` specifies `vertex.region: global`, an existing or defaulted `VERTEX_REGION=us-central1` in `~/.hermes/.env` overrides it silently, causing 404 errors on Vertex AI. The launch script must ensure `VERTEX_REGION=global` is written to `~/.hermes/.env`.
  3. **Hermes CLI Flag Incompatibility (`--create-if-missing`):**
     Scion's default harness config for Hermes configured `command.base: ["hermes", "chat", "--yolo", "-c", "scion-session", "--create-if-missing"]`. However, the Hermes CLI does not recognize `--create-if-missing` (it only supports `-c / --continue`). Passing this unrecognized flag caused the container entrypoint to immediately exit with code 2. The solution is the wrapper pattern `base: ["sh", "/home/scion/.local/bin/hermes-launch.sh"]` with `task_flag: "--prompt"` and `task_position: "before_base_args"`.
  4. **Scion Hosted Harness Config Caching (`~/.scion/cache/harness-configs/`):**
     In Scion's hosted/broker architecture, the Runtime Broker's `HCResolver` resolves and caches harness configurations under `~/.scion/cache/harness-configs/<contentHash>/config.yaml`. Updating only `~/.scion/harness-configs/` does not update running or restarted containers if SQLite `hub.db` or cached hashes point to the bundled configs in `workspace-storage/hubs/<hub-id>/harness-configs/global/`. To update a harness persistently, the harness config must be updated in `hub.db`, `workspace-storage`, and the broker cache cleared or updated.
  5. **Antigravity Role Directory Detection:**
     In Scion project staging, agent configurations are organized by agent role name under `~/.scion/project-configs/<project>__<id>/.scion/agents/<role>/home/`. The previous provisioner hook patch script only checked agent names, missing custom role directories or misidentifying role folders during provisioner patching.
- **Fix:**
  - Automated patching via [`scripts/patch-antigravity-hooks.py`](../scripts/patch-antigravity-hooks.py) and [`scripts/patch-opencode-and-hermes.py`](../scripts/patch-opencode-and-hermes.py).
  - Configured `model: "Gemini 4 Argon (High)"` and `location: global` across all 6 Antigravity agent templates and container `settings.json`.
  - Configured OpenCode catalog `models.json` with `gemini-4-argon` and `gemini-3.8-flash`, setting `GOOGLE_CLOUD_LOCATION=global` and `model: google-vertex/gemini-4-argon`.
  - Added wrapper [`templates/reviewer/home/.local/bin/hermes-launch.sh`](../templates/reviewer/home/.local/bin/hermes-launch.sh) for Hermes, setting `VERTEX_REGION=global` and launching `hermes chat --yolo --accept-hooks --provider vertex -m google/gemini-4-argon`.
  - Pushed updated templates to Hub via [`scripts/push-templates.py`](../scripts/push-templates.py).
- **Encoded in:**
  - [`templates/backend/home/.local/bin/opencode-launch.sh`](../templates/backend/home/.local/bin/opencode-launch.sh)
  - [`templates/reviewer/home/.local/bin/hermes-launch.sh`](../templates/reviewer/home/.local/bin/hermes-launch.sh)
  - [`templates/frontend/scion-agent.yaml`](../templates/frontend/scion-agent.yaml)
  - [`templates/eval-deployer/scion-agent.yaml`](../templates/eval-deployer/scion-agent.yaml)
  - [`templates/eval-lead/scion-agent.yaml`](../templates/eval-lead/scion-agent.yaml)
  - [`templates/factory-analyst/scion-agent.yaml`](../templates/factory-analyst/scion-agent.yaml)
  - [`templates/search-evaluator/scion-agent.yaml`](../templates/search-evaluator/scion-agent.yaml)
  - [`templates/ui-craftsman/scion-agent.yaml`](../templates/ui-craftsman/scion-agent.yaml)
  - [`scripts/patch-antigravity-hooks.py`](../scripts/patch-antigravity-hooks.py)
  - [`scripts/patch-opencode-and-hermes.py`](../scripts/patch-opencode-and-hermes.py)
  - [`scripts/push-templates.py`](../scripts/push-templates.py)

## L27. Cross-process trace state (`tracestate.go`) and stacked parent-child conversation traces in Cloud Trace

- **When:** 2026-10-15.
- **Symptom:** Traces from agent containers appeared in Cloud Trace, but every hook invocation (`agent.turn.start`, `agent.tool.result`, `gen_ai.api.response`, `agent.turn.end`) showed up as an isolated ~0ms root span with `parentSpanId: null` and a random `traceId`. Operators could not expand a waterfall of a turn or follow a multi-turn conversation path.
- **Root Causes:**
  1. **Ephemeral Hook Processes + In-Memory `sync.Map`:** Every harness hook (`PreInvocation`, `PostInvocation`, `PreToolUse`, `PostToolUse`, `Stop`) executes as a separate short-lived `sciontool hook` process. `TelemetryHandler` stored in-flight spans in an in-memory `sync.Map` (`h.spanStore`). Because `startSpan` never called `span.End()`, start events (`PreInvocation`, `PreToolUse`) were discarded when their hook process exited; end events (`PostInvocation`, `PostToolUse`, `Stop`) ran in a fresh process where `h.spanStore` was empty and fell back to `singleSpan(context.Background())` — emitting a ~0ms root span with a brand-new `TraceID`.
  2. **Claude Code Native OTLP Logs Lacked Trace Context & Child Spans:** Claude Code emits `api_request` events as native OTLP logs to `127.0.0.1:4317` without a `TraceId`/`SpanId`, so Claude API calls were invisible inside the turn's Cloud Trace waterfall.
  3. **Antigravity `PostToolUse` Omitted `tool_name`:** Antigravity's `PostToolUse` hook payload only carries `response` (omitting `toolCall.name`), so without state persisted from `PreToolUse`, `agent.tool.result` spans had empty `tool_name`.
  4. **Sticky `/tmp` Ownership Between `root` and `scion`:** Container lifecycle hooks (`post-start`) run as `root` (`uid=0`), whereas harness hooks run as `scion` (`uid=1000`). Writing `/tmp/scion-trace-state.json` with `0600` or via `os.Rename` failed across `root`/`scion` in sticky `/tmp` (`1777`).
- **Fix:**
  1. **Cross-Process `TraceState` (`/tmp/scion-trace-state.json`):** Implemented `pkg/sciontool/telemetry/tracestate.go` with `syscall.Flock` locking and world-rw (`0666`) in-place truncation (`O_CREATE|O_RDWR|O_TRUNC`), plus `ExplicitIDGenerator` so `sdktrace.TracerProvider` honors persisted `TraceID`, `TurnSpanID`, `PendingModel`, and `PendingTools` across hook processes.
  2. **Hierarchical Turn Waterfall (`agent.turn/<agent_slug>`):**
     - Root span `agent.turn/<agent_slug>` (`SpanID = TurnSpanID`, `ParentSpanID = nil`) spans `[TurnStartTime, TurnEndTime]` with summary attributes (`turn.index`, `turn.model_calls`, `turn.tool_calls`, cumulative token usage, `scion.previous_trace_id`) and an OTel **Span Link** (`link.type = "previous_turn"`) to the previous turn in the conversation.
     - Child spans (`ParentSpanID = TurnSpanID`) share the turn's `TraceID` and real `[StartTime, EndTime]` durations: `agent.turn.prompt`, `gen_ai.api.response/<model>`, `agent.tool.result/<tool_name>`, and `agent.status/<status_type>` (which nests 3 levels deep under the active tool span when `sciontool status` runs inside a tool call).
  3. **Claude Code Log-to-Span Synthesis & Cloud Logging Correlation:** `CorrelateAndSynthesizeClaudeSpans` attaches the active turn's `TraceID`/`SpanID` to native OTLP logs and synthesizes `gen_ai.api.response/<model>` child spans for `api_request`/`api_error`. `protoLogToCloudEntry` sets top-level `entry.SpanID`, `entry.TraceSampled = true`, and `SCION_GCP_PROJECT_ID` fallback so logs appear inline inside Cloud Trace waterfalls.
  4. **Safe Live Deployment:** Update `/usr/local/bin/sciontool.real` in running containers via `docker cp` ([`scripts/patch-sciontool-telemetry.sh`](../scripts/patch-sciontool-telemetry.sh)) — never run `docker restart` on `--rm` Scion containers.
- **Encoded in:** [`scripts/patch-sciontool-telemetry.sh`](../scripts/patch-sciontool-telemetry.sh), [`docs/telemetry/reproduce-telemetry.md`](telemetry/reproduce-telemetry.md).

## L28. Several factories in one monorepo: names, start folders, and what the e2e showed

- **When:** 2026-10-22, on a throwaway single-node Hub (Scion `nightly-20261022`) with a private monorepo
  (`src/project1..3`), two factories × (`lead` on Claude Code, `eval-lead` on Antigravity), plus one shared-mode lead.
- **Problem:** several factories on one Hub need distinct agent names (bare `lead` in every project makes the CLI exit 2,
  "agent name found in several projects", and every clone-per-agent lead pushes `scion/lead`), and each agent should
  start in its product folder while keeping the whole repository. Scion has no working-directory setting: the Docker
  runtime always sets `--workdir /workspace` (`pkg/runtime/common.go`), and a relative `--workspace <subdir>` mounts only
  that subtree.
- **Fix:**
  1. `AGENT_PREFIX` renders every peer reference as `${<ROLE>_AGENT}` → `<prefix>-<role>`; start agents with
     `render-templates.py --agent-names`.
  2. `FACTORY_SUBDIR` + [`factory-cd.sh`](../config/harness/home/.local/bin/factory-cd.sh) in custom-named
     `<harness>-factory` harness-configs ([`make-factory-harness-configs.py`](../scripts/make-factory-harness-configs.py)).
     Bundled configs are refreshed from the binary on every Hub restart, so they are never edited.
- **Evidence (e2e):** `scion list` showed `project1-lead` / `claude-factory` and `project1-eval-lead` /
  `antigravity-factory`, containers `project1--project1-lead`; `readlink /proc/<pid>/cwd` was `/workspace/src/project1`
  for both the `claude` and the `agy` process (`/workspace/src/project3` in shared mode); `ls /workspace/src` listed all
  three products; four distinct `scion/<agent>` branches pushed; no trust prompt; Antigravity status hooks fired from the
  product folder (activity `completed`), `git status` stayed clean; Claude on Vertex answered an admin message.
- **Gotchas found:**
  - Messaging is per Hub project: `scion message project1-eval-lead` from `project1-lead` → `agent_not_found` (404).
  - `push-templates.py` fails on a fresh Hub (`default home directory not found`): the default template is stored as
    blobs. `scion templates sync <role> --project=<slug>` works.
  - `scion hub projects create` from the same URL a second time needs `--yes` when not on a TTY; shared mode clones at
    creation and needs `githubToken` through the API (the CLI has no token flag).
  - In clone-per-agent mode the leads flagged that `_shared/agents.md` describes a shared checkout on `main`.
  - `deploy.sh` in a new org project: enable `cloudresourcemanager.googleapis.com` first, create the `default`
    network if the org skips it, and re-run after a transient IAP SSH `502` or a "service account does not exist"
    right after creating it (the script is idempotent). Its `build` image source builds only Antigravity; build
    `scion-claude` on the VM for Claude Code agents, and enable the Claude model in Model Garden per project
    (otherwise the harness starts but answers "model … is not available on your vertex deployment").
- **Second e2e (shared directories and knowledge planes, runbook M1–M8 run as written on a fresh Hub):**
  - `POST /api/v1/projects` has no branch field: the branch goes in the `scion.dev/default-branch` label, else the
    Hub clones `main` (the eval projects first came up on `main`). `scion-admin.py project-create` sends the labels.
  - The Hub stores `githubToken` as the project's `GITHUB_TOKEN` secret with injection mode `always`.
  - New Hub projects already have a `scratchpad` shared directory; `shared-dirs --apply` added `knowledge-plane`.
  - `shared-dirs/` on the broker appears only when the first agent starts, so the watchdog now fills
    `knowledge-plane` as soon as the project's `project-configs/<slug>__<id8>/` exists; the agents' first turn
    already read `KP-project1` / `KP-project2`. Scion keeps the pre-filled folder when it mounts it.
  - Inside every agent: `/scion-volumes` = `knowledge-plane scratchpad`; each factory saw its own product's
    knowledge plane; writes to it were refused; the scratchpad is per Hub project and appears in the Hub's Files
    API (the tabs next to `workspace`); an edit to the source reached a running agent on the next watchdog run;
    the cron run kept `KNOWLEDGE_PLANE_SOURCES` from `watchdog.env`.
  - A runbook loop that relied on bash word splitting (`set -- $p`) broke in zsh, macOS's default shell: runbook
    commands are now written out one per project.
- **Third e2e (README end to end, 2026-10-23):** a fresh project, the Hub from the README, two factories compiled
  from interviews against sample repositories (Go, 10 agents; Python, 5 agents) and one monorepo factory, 17 agents
  on one `small` VM (3.2 GB of 16 GB used). Found and fixed:
  - The newest nightly (`nightly-20261023`) doesn't start on a fresh install (the Hub refuses its default
    `http://localhost:8080` base URL before the installer configures IAP). The README pins the tested release for
    both the installer checkout and `--version`; re-running the installer with the pinned version repaired the VM.
  - Repository detection missed Python services that have only `requirements.txt`.
  - The default interview roster used OpenCode and Hermes, which need harness patches: OpenCode stopped at a
    permission prompt and the bundled Hermes config crashed (`--create-if-missing` rejected). Defaults are now
    Claude Code and Antigravity; recompiling and re-pushing two roles fixed the running factory.
  - Two kits on one Hub: each kit's `KNOWLEDGE_PLANE_SOURCE_PROJECT` would have sent one factory's knowledge plane
    to every project, and a monorepo `KNOWLEDGE_PLANE_SOURCES` map skipped every unmapped factory. The watchdog now
    gives each factory with its own `knowledge-plane/` that one.
  - `FACTORY_PROJECTS=… scripts/run-on-hub.sh …` was overridden by the env file; command-line values now win.
  - `seed-github-waves.sh` never created issues (fixed before the run; created 3 issues live).
  - Docs: build an image for every harness in the roster, set `DATA_BUCKET`, keep one env file per factory
    (`FACTORY_ENV`), shallow-clone Scion.
  Verified: preflight on a new and on a shared Hub, `push-templates.py --templates <kit>`, `scion-admin.py scion
  start`, the lead on Claude Opus working the seeded wave issues, every eval team reading its own knowledge plane,
  and the README cleanup of one factory leaving the others running.
- **Encoded in:** [`scripts/render-templates.py`](../scripts/render-templates.py),
  [`scripts/make-factory-harness-configs.py`](../scripts/make-factory-harness-configs.py),
  [`config/harness/home/.local/bin/factory-cd.sh`](../config/harness/home/.local/bin/factory-cd.sh),
  [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py), [`scripts/scion-admin.py`](../scripts/scion-admin.py)
  (`project-create`, `shared-dirs`), [setup-runbook.md](setup-runbook.md#several-factories-in-one-monorepo).

## L29. A real monorepo (Kubernetes) as three factories, and leads that start their own teams

- **When:** 2026-10-23, on a new single-node Hub (`nightly-20261022`, `medium`) that three factories then joined
  through the existing-Hub path. The repository was a fork of `kubernetes/kubernetes` (~1.3 GB). The three factories
  were `kubelet` (`pkg/kubelet`), `scheduler` (`pkg/scheduler`) and `kubectl` (`staging/src/k8s.io/kubectl`), each
  compiled from an interview kit with `monorepo: {subdir, agent_prefix}`. That made 6 Hub projects and 15 agents.
  The build branch was `factory/main` and the orphan eval branch `factory/eval`; `master` was never written.
- **Problem 1, a flat team:** the PO started all 15 agents, so the Hub showed them side by side. No agent was the
  team's parent, the leads got none of their team's notifications (L5), and a team member had no explicit reporting
  line.
- **Fix 1, chain of command:** the PO starts only `<slug>-lead` and `<slug>-eval-lead`. Each lead's prompt has a
  "Chain of command" table with the exact `scion start <prefix>-<role> -t <role> --harness-auth vertex-ai …` for
  every teammate. At each wake-up the lead starts the missing ones, resumes stopped ones, and starts nothing else.
  Scion records the creator (ancestry `PO -> lead -> backend`), so the project's tree view shows the lead on top and
  the Hub routes the team's notifications to it. Every team member's prompt says "You report to `<lead>`" and forbids
  starting agents. The project message mode stays `project`, so a backend can still hand straight to the reviewer
  when the brief says so.
- **Evidence:**
  - Inside the lead's container, `scion list --lineage` listed the backend and reviewer under each of the 6 leads,
    within 5 minutes of the PO starting only the leads.
  - Wave 2 (#4–#6) then ran through that hierarchy: the leads posted plans, the backends wrote the tests, the reviewers
    gave PASS, and commits `32f648d`, `f5db068` and `e2f5737` reached `factory/main`.
  - An independent `go test` at the head passed for all three packages, and the issues stayed open for the PO.
  - Wave 1 (flat team) had also delivered: `36a3cc2`, `6119bea` and `9852dd6`. Each factory changed only files under
    its folder, and the factories rebased on each other's pushes to the shared branch.
- **Problem 2, who closes:** in wave 1, `kubelet-lead` closed #1 itself while the other leads left theirs open. The
  compiled kit didn't say that closing the issue is the PO's acceptance.
- **Fix 2:** the kit's backlog section says it now ("The PO accepts work by closing the issue … never close an
  issue yourself"). In wave 2 all three issues stayed open at `status:verified`.
- **Gotchas found and fixed:**
  - **Knowledge-plane links:** links from `<subdir>/knowledge-plane` to repo-root sources were one level short per
    path part. `kp-check` now passes at every depth.
  - **Shared backlog:** three factories share one repository's issues, so seeded issues and every
    `gh issue list` / `gh issue create` carry `factory:<prefix>`.
  - **Seed dry run:** `seed-github-waves.sh` created labels even in a dry run.
  - **Issues on forks:** forks have Issues turned off by default (`gh repo edit --enable-issues`).
  - **Hub write timeout:** the Hub's HTTP write timeout (60 s) closes `POST /api/v1/projects` while it clones a large
    repository (~75 s here), but the clone finishes. `scion-admin.py project-create` now waits for the seeded
    checkout (`.scion/project-id` matching the project row) or reports a rollback, and never posts twice.
  - **`setup-sandbox.sh` races:** it now retries IAM bindings while a new service account propagates, and treats an
    index created by a parallel run (`ALREADY_EXISTS`) as done.
- **Gotchas in `deploy.sh` (documented, not ours to fix):**
  - It always takes the first zone of the region. On a `ZONE_RESOURCE_POOL_EXHAUSTED` stockout, change the region.
  - Its proxy firewall rule (`scion-hub-<name>-allow-proxy`) keeps the subnet range of the first attempt's region and
    only warns about the drift. After a region change, the IAP URL answered `Bad Gateway`, and the Cloud Run log said
    `dial tcp <vm-ip>:8080: i/o timeout`. Update the rule's source range to the new region's `default` subnet.
  - With certificate-based access, `gcloud compute scp` failed intermittently (`ECPProxyError`). Re-running the
    installer was enough.
- **Kubernetes facts:**
  - The images ship Go 1.26.1 and the repository needs `go 1.27.0`. `GOTOOLCHAIN=auto` downloaded go1.27.0 on the
    first `go test` in each container (15–105 s), with no change needed.
  - Each `go test` of one package compiled in under two minutes on `medium`.
  - Six shared clones used about 9 GB of disk.
- **Teardown that keeps the Hub (2026-10-26):**
  - `scion hub projects delete <slug> --yes --force` removed the agents and the shared checkouts.
  - The Hub left a 36-byte marker per slug in `~/.scion/hub-workspaces/<slug>`, holding the deleted project's ID.
    Remove them by hand before you reuse a slug.
  - `setup-sandbox.sh` has no reverse. Remove its project bindings first (the deployer and runtime service accounts;
    `serviceUsageConsumer` and `datastore.user` on the Hub VM's service account; `cloudbuild.builds.builder` and
    `datastore.viewer` on the default compute service account), then delete the service accounts, the data bucket
    and the Firestore database. Removing the bindings first avoids leaving `deleted:serviceAccount:` members behind.
  - The roles that `deploy.sh` grants the Hub VM stay (`aiplatform.user`, `logging.logWriter`, and the others).
  - Forks: `gh issue delete`, delete the `factory/*` refs and the labels, then `gh repo edit --enable-issues=false`.
- **Encoded in:** [`scripts/scion_factory.py`](../scripts/scion_factory.py) (chain of command, `factory:<prefix>`,
  close rule), [`scripts/knowledge_plane.py`](../scripts/knowledge_plane.py),
  [`scripts/scion-admin.py`](../scripts/scion-admin.py), [`scripts/setup-sandbox.sh`](../scripts/setup-sandbox.sh),
  [`templates/lead`](../templates/lead/system-prompt.md), [`templates/eval-lead`](../templates/eval-lead/system-prompt.md),
  README step 8, [setup-runbook.md](setup-runbook.md) steps 8 and M8.

## L30. Multi-app repositories, the 13-agent specialist fabric (`deploy: none`), and autonomous self-healing

- **When:** 2026-10-26, running the specialist agent fabric against `duboc/bananacast` (a multi-app repository with
  `src/backend` in Python/Flask, `src/frontend` in React/Vite TypeScript + a FastAPI reverse proxy, `src/management`
  in Next.js TypeScript, and `src/infrastructure/terraform` in HCL) on an existing single-node Scion Hub with
  `infrastructure.deploy: none` and `team.autonomy: standard`.
- **Roster (13 agents across 2 Hub projects, spawned by the 2 leads):**
  - `bananacast` (10 build agents): `lead`, `backend`, `reviewer`, `researcher`, `doc-writer`, `investigator`,
    `security-auditor`, `frontend`, `test-engineer`, `migration-specialist`.
  - `bananacast-eval` (3 eval agents): `eval-lead`, `domain-evaluator`, `safety-evaluator`.
- **What the specialist fabric caught and shipped on `factory/main` (`main` untouched):**
  - **Root-causing two 5-month-old security issues (`#305` and `#301`) that didn't reproduce in Flask:**
    - On `#305` (`/openapi.json` publicly exposed), `security-auditor` and `safety-evaluator` independently discovered
      that `src/frontend/Dockerfile` runs a Python FastAPI reverse proxy (`src/frontend/proxy.py`) where
      `FastAPI(docs_url=None, redoc_url=None)` omitted `openapi_url=None`, exposing `/openapi.json` before the SPA
      catch-all route. `backend` fixed both `src/backend/app.py` and `src/frontend/proxy.py` with regression tests,
      `reviewer` and `security-auditor` both rendered `PASS` (`/scion-volumes/scratchpad/reports/305-review.md`,
      `305-security-audit.md`), and `5e39085` landed on `factory/main` (`#305` -> `status:verified`).
    - On `#301` (`LFI via /..%2f traversal`), `investigator` and `safety-evaluator` traced the vulnerability to the same
      `src/frontend/proxy.py` (`proxy_assets` and `serve_spa` joined URL-decoded paths to `DIST_DIR` without checking
      `.resolve().is_relative_to(DIST_DIR.resolve())`), wrote `/scion-volumes/scratchpad/reports/301-investigation.md`,
      and handed remediation to `backend`. `reviewer` and `security-auditor` both rendered `PASS`, and `476b5d3` landed
      on `factory/main` (`#301` -> `status:verified`).
  - **Cross-app feature delivery (`#321`) + caller security audit:**
    - For `#321` (`CampaignEndedPage` when lifecycle is `archived` or `endDate` has passed), `frontend` built
      `src/frontend/components/CampaignEndedPage.tsx` and management branding fields (`src/management/`), while `backend`
      implemented `GET /api/config/<slug>` with a whitelisted `serialize_ended_branding()` envelope and discovered during
      its caller audit that `POST /api/users/lookup` and `POST /api/performances` previously allowed starting sessions on
      expired campaigns — closing that gap with `403 CAMPAIGN_ENDED` (`0e30d8b`, `#321` -> `status:verified`, all 4 Wave 1
      issues verified).
  - **Human-on-the-loop `status:po-decision` (`#327`, `#331`) without stalling the factory:**
    - On its first wake-up, `lead` saw that `#296` (LGPD consent copy) explicitly asked the PM to pick between two legal
      wordings and that `src/frontend/` had no `locales/{pt,en,es}.json` yet (requiring an ADR under
      `autonomy: standard`). `lead` opened `#327` (`status:po-decision`, `factory:bananacast`) with 3 concrete options
      and a recommendation (`1A, 2A`), posted the daily digest on `#326`, and kept WIP at 2/2 (`#301` and `#305`) while
      `researcher` wrote a 31 KB pre-work brief (`/scion-volumes/scratchpad/reports/296-321-research.md`).
    - Once the PO answered `1A, 2A` on `#327` (`status:approved`), `doc-writer` recorded `ADR-002` (`929186e`),
      `frontend` and `test-engineer` coordinated directly via `scion message` to ship `src/frontend/locales/{pt,en,es}.json`,
      `src/frontend/i18n.ts`, and `src/backend/tests/test_locale_parity.py` (`47f81b5`, `#296` -> `status:verified`),
      and `doc-writer` followed up with `docs/i18n.md` (`29b75af`, "docs follow code").
  - **Independent code-pyramid evaluation (`deploy: none`) and `eval-finding` loop:**
    - `eval-lead`, `domain-evaluator`, and `safety-evaluator` graded commits `1aca349`, `5e39085`, and `47f81b5` across
      L1–L5, recorded runs in Firestore (`eval_runs`), and filed `eval-finding` issue `#328` (`59` `pip-audit`
      advisories in `src/backend/requirements.txt`), which `lead` picked up and assigned to `researcher`.
- **Four gaps found and fixed during the run:**
  1. **Multi-app repository inspection (`inspect_repository`):** repositories without a root manifest that organize code
     under `src/backend`, `src/frontend`, `src/management` returned `primary_language: "unknown"`. `inspect_repository`
     now scans conventional app subdirectories (`src/*`, `apps/*`, `services/*`, `packages/*`), records
     `detected["apps"]`, detects Flask, and scopes commands (`cd src/backend && pytest -q`).
  2. **Per-language test-ratio evidence (`discovery.py`):** a Python backend with 18 pytest files next to a 100-file
     TypeScript frontend with 0 tests previously reported `low test ratio: 0/100`. `discovery.py` now evaluates test
     ratios per language (`low test ratio (typescript): 0 test files / 100 source files`).
  3. **Watchdog false `PO_ESCALATION` alerts on Scion `state-change` messages (`factory-watchdog.py` Check 4):**
     because the PO starts `lead` and `eval-lead`, the PO is their parent in Scion's lineage tree, so Scion sends
     `type = 'state-change'` rows (`COMPLETED`, `STALLED`) to `user:<id>` at the end of every turn. `check_po_inbox`
     previously paged `PO_ESCALATION` on every row; it now ignores `type == 'state-change'` while advancing the bookmark,
     alerting the human PO only on explicit `instruction` escalations.
  4. **Hung child subprocesses inside running containers (`factory-watchdog.py` Check 3 + `role_library.py`):**
     `investigator` ran a one-liner that started an in-process `uvicorn.Server` and called blocking `socket.recv()` on
     the same `asyncio` event loop, deadlocking PID `506` for 15 minutes while the container itself stayed `Up` and
     `running`. Role protocols now require in-process test clients (`TestClient`, `app.test_client()`) or `timeout 30s`,
     and `Check 3` in `factory-watchdog.py` now inspects `ps -eo pid,etimes,comm,args` in running containers, reaps
     non-harness child commands older than `WATCHDOG_SUBPROCESS_TIMEOUT_SEC` (10 min), and messages both the worker and
     its lead to continue autonomously.
- **Encoded in:** [`scripts/scion_factory.py`](../scripts/scion_factory.py),
  [`scripts/discovery.py`](../scripts/discovery.py), [`scripts/role_library.py`](../scripts/role_library.py),
  [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py), [`README.md`](../README.md).

## L31. 20-hour autonomous 30-issue sweep: shared-worktree commit slots, token-scope graceful degradation, IaC specialists, and `eval-sweep` guarding

- **When:** 2026-10-26 to 2026-10-27, running the 13-agent specialist fabric unattended for 20 hours across the full
  30-issue backlog (`#112`–`#331`, plus `eval-finding` `#328`) of `duboc/bananacast` on `factory/main` / `factory/eval`
  (`main` untouched).
- **Outcome (evidence):**
  - **29 of 30 issues verified (`L1–L5 PASS`) and closed** across **31 commits** and **511 Scion messages**, expanding
    the backend test suite from ~250 tests to **476 passed (+ 328 subtests)**, adding a frontend `vitest` suite, and
    reducing `pip-audit` advisories from `59` to `0` (`ruff` 0 errors, `tsc --noEmit` 0 errors).
  - Only `#234` (`.github/workflows/{ci,deploy}.yml`) stayed open (`status:blocked`) paired with PO inbox decision
    `#332` (`status:po-decision`) because GitHub rejected pushing workflow files without `workflow` PAT scope; `lead`
    saved `/scion-volumes/scratchpad/pending-234/234-workflows.patch`, landed the permitted Terraform WIF + TypeScript
    changes (`af6e8a6`), and completed `#235`, `#262`, `#268`, `#270`, `#297`, `#298`, and `#299` without stalling WIP.
- **Six operational gaps uncovered and codified:**
  1. **Watchdog schedule guard missed `eval-lead`'s `eval-sweep-*` schedule (`factory-watchdog.py` Check 2):**
     `check_heartbeat_schedules` queried `WHERE s.name LIKE '%-lead-heartbeat'` without filtering `FACTORY_PROJECTS`, so
     `watchdog.log` reported `1 heartbeat schedules active` while leaving `eval-sweep-factory-main` unguarded (and
     touching schedules outside `FACTORY_PROJECTS` on shared Hubs). `Check 2` now scopes to `FACTORY_PROJECTS`, ignores
     `deleted` rows, and guards both `%heartbeat%` and `eval-sweep%` schedules. `load_runtime_env` also loads
     `watchdog.env` before `sync.env`, `watchdog_env_content` + `run-on-hub.sh` persist
     `WATCHDOG_SUBPROCESS_TIMEOUT_SEC`, and `Check 5` uses `rsync -ai --delete` to report `up to date` when unchanged.
  2. **`git stash push -u` and unconditional `git pull --rebase` in shared worktrees (`templates/_shared/agents.md`,
     `scion_factory.py`):**
     Under `WIP=2` in a shared `/workspace` checkout, untracked `.scion/` and `.agents/` harness directories sit next to
     concurrent workers' active edits. Running `git stash push -u` sweeps up harness metadata and teammates' files, and
     hardcoding `build_branch` in shared `agents.md` misdirected `eval` agents. Commit-slot rules now check
     `git fetch origin <branch> && git rev-list --count HEAD..origin/<branch>` first (skipping `rebase`/`stash` when
     `0`), require explicit `git add -- <files>` verified with `git diff --cached --name-only`, forbid `git stash -u`
     and staging `.scion/` / `.agents/`, and allow lead-authorized co-commits when two reviewed `PASS` issues touch the
     same file (`#302+#303`, `#145+#308`, `#126+#142`, `#217+#218`, `#220+#221`, `#297+#298+#299`).
  3. **Graceful partial delivery on GitHub token-scope push rejections (`#234` / `#332`):**
     When GitHub rejects a push because a path requires elevated token permissions (such as `.github/workflows/*`
     without `workflow` scope), workers now reset the rejected commit, save the reviewed restricted files as a patch
     under `/scion-volumes/scratchpad/pending-<issue>/`, push the permitted files, and escalate the patch path to the PO
     (`status:po-decision` with Git) while keeping remaining backlog WIP moving.
  4. **`migration-specialist` needed Terraform/IaC and CI workflow guardrails (`role_library.py`):**
     `discovery.py` proposes `migration-specialist` whenever `.tf` files exist, and overnight it delivered 7 major
     Terraform/GCS/Secret Manager/Monitoring/WIF/Cloud Armor slices, yet its library protocol previously mentioned only
     SQL DDL. Its responsibility and protocol now cover `terraform fmt -check`,
     `terraform init -backend=false && terraform validate`, opt-in feature flags (`default = false`) when live deploy is
     disabled, Secret Manager over plaintext `tfstate`, pinned CI action SHAs, and never running `terraform apply`
     without authorization.
  5. **Optional-dependency guards in multi-app Python test suites (`pytest.importorskip`):**
     On `#270`, placing a test under `src/backend/tests/` that imported `fastapi` (installed in
     `src/frontend/proxy_requirements.txt`, not `src/backend/requirements.txt`) broke `pytest -q` collection in minimal
     backend environments until `pytest.importorskip("fastapi")` was added (`22f2660`). `backend`, `test-engineer`, and
     `reviewer` protocols now require guarding cross-app or optional-dependency test imports.
  6. **Hub token preflight check & PAT `workflow` scope documentation (`hub-preflight.py`, `README.md`,
     `setup-runbook.md`):**
     `hub-preflight.py` now checks `~/.factory-github-token` existence/non-zero size without reading secret bytes and
     warns when missing, and Step 6 / Step 5 document when to include vs intentionally omit `workflow` scope.
- **Encoded in:** [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py),
  [`scripts/run-on-hub.sh`](../scripts/run-on-hub.sh), [`templates/_shared/agents.md`](../templates/_shared/agents.md),
  [`scripts/scion_factory.py`](../scripts/scion_factory.py), [`scripts/role_library.py`](../scripts/role_library.py),
  [`scripts/hub-preflight.py`](../scripts/hub-preflight.py), [`README.md`](../README.md),
  [`docs/setup-runbook.md`](setup-runbook.md).


## L32. GKE Workload Identity & Cloud Run API Variations

**The Failure:** Hardcoding authentication and probe paths failed when migrating Scion hubs between Cloud Run and GKE Autopilot. We encountered cold Autopilot `503` responses crashing client initialization, broken Workload Identity mounts due to default GCP identity misconfigurations, and state-loss bugs when K8s CronJobs tried to maintain cursor positions locally.

**The Fix:**
- GKE uses `block` default identity and explicit Workload Identity KSA mapping (for `scion-worker` and `scion-deployer`).
- Cloud Run health checks must use `/health` (not `/healthz` which works on k8s).
- Our Scion Hub client handles cold Autopilot `503` idempotency by polling during `ensure_agent_started`.
- We maintain stateless K8s CronJob states (like watchdogs) remotely in Firestore to survive pod rotation.
- **Encoded in:** [`scripts/hub_client.py`](../scripts/hub_client.py), [`scripts/setup-gke-hub.sh`](../scripts/setup-gke-hub.sh), [`scripts/setup-sandbox.sh`](../scripts/setup-sandbox.sh), [`scripts/install-k8s-control-plane.py`](../scripts/install-k8s-control-plane.py), [`scripts/provision_factory.py`](../scripts/provision_factory.py), and [`docs/gke-setup-runbook.md`](gke-setup-runbook.md).

## L33. Live Full GKE Multi-Hub HA & Kubernetes Monorepo Auto-Factory E2E

- **When:** 2026-10-28, provisioning a Full GKE Multi-Hub HA Scion deployment (`deploy/terraform/` — Cloud Run Gen2 Hub, Cloud SQL PostgreSQL, Filestore RWX NFS, Artifact Registry multi-arch images, and GKE Autopilot agent cluster) from scratch and running `./scripts/scion-factory provision --apply --go` against a fork of `kubernetes/kubernetes` scoped to `staging/src/k8s.io/apimachinery/pkg/util/version` (`infrastructure.deploy: none`, `infrastructure.runtime: kubernetes`, `antigravity` / `Gemini 4 Argon (High)`).
- **Outcome (live evidence):**
  - Cloud Run Gen2 HA Hub (`iap_enabled = true`) came up healthy with zero static K8s Secrets or PATs.
  - All 9 phases of `scion_factory.py provision --apply` succeeded idempotently (`preflight`, `sandbox`, `seed_repo`, `projects`, `identity_and_profile`, `shared_dirs_and_kp`, `harness_and_templates`, `control_plane`, `schedules_and_leads`).
  - `k8s-version-lead` and `k8s-version-eval-lead` booted on GKE Autopilot, authenticated to Vertex AI via Workload Identity (`scion-worker` KSA), and `k8s-version-lead` autonomously spawned its 5 specialist child pods (`k8s-version-backend`, `k8s-version-reviewer`, `k8s-version-researcher`, `k8s-version-doc-writer`, `k8s-version-investigator`) over Cloud Run Direct IAP, wrote `/scion-volumes/scratchpad/briefs/8-k8s-version-backend.md`, and moved Wave 1 Issue `#8` to `status:in-progress`.
  - Both K8s `CronJob`s (`scion-factory-sync` every 1m and `scion-factory-watchdog` every 5m) completed in `5s–9s`, writing all GKE pods (including `parent_agent: "k8s-version-lead"`) to Firestore `factory_agent_status` and persisting watchdog state in `factory_control_state/watchdog__default`.
- **Six real-world Cloud Run HA + GKE Autopilot discrepancies uncovered and codified:**
  1. **Two-pass Cloud Run URL + Direct IAP bootstrap (`setup-gke-hub.sh`):**
     Upstream `configurations/hub` requires `hub_domain` and `oidc_audience` at container startup before Cloud Run allocates a service URL. `setup-gke-hub.sh` discovers the deterministic `<name>-<project_number>.<region>.run.app` URL (or inspects `google_cloud_run_v2_service.hub`), enables Cloud Run Direct IAP (`iap_enabled = true`), grants `roles/iap.httpsResourceAccessor` to `HUB_IMPERSONATE_SA`, `CONTROL_SA`, and `WORKER_SA`, and applies Pass 2 once the Google-managed Direct IAP client (`369001918367-<CLIENT_ID>.apps.googleusercontent.com`) is provisioned.
  2. **Zero-secret Google-managed Cloud Run Direct IAP auth (`hub_client.py` + `factory-cd.sh`):**
     Google-managed Cloud Run Direct IAP (`369001918367-*`) rejects OIDC ID tokens unless a custom OAuth client is manually bound, and instead accepts Service Account OAuth2 access tokens in `Proxy-Authorization: Bearer <access_token>` — **only when** GKE Workload Identity's `gke-metadata-server` is queried with explicit `?scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid` (omitting `userinfo.email` + `openid` fails with IAP HTTP `401` Error code `90`) and a `>= 10s` cold-pod metadata retry. Inside agent pods, `factory-cd.sh` writes `~/.scion/iap-access-token`, refreshes it every 25 minutes, exports `SCION_TRANSPORT_TOKEN_FILE`, and unsets `SCION_TRANSPORT_TOKEN` (otherwise `transportauth.FileSource.pickLocked()` prefers the bootstrap JWT's parsed `exp` over an opaque OAuth2 access token).
  3. **Four upstream Scion (`GoogleCloudPlatform/scion`) HA + GKE runtime bugs patched idempotently in `setup-gke-hub.sh`:**
     - `pkg/runtimebroker/nfs_mount.go`: Cloud Run Gen2 mounts Filestore in `/proc/mounts` with device `/var/lib/volumes/nfs/nfs` rather than `server:export` (`503 project_storage_unavailable`).
     - `pkg/agent/run.go`: Externalized `clone-per-agent` workspace path `~/.scion/project-configs/<slug>__<id8>/.scion/agents/<name>/workspace` was omitted from `ValidateWorkspaceSource` `roots`.
     - `pkg/hub/handlers_projects_core.go`: `POST /api/v1/projects` skipped `initHubManagedProject` for `per-agent` Git projects (`409 project configuration missing` until Hub restart).
     - `pkg/runtime/k8s_runtime.go`: Setting `PersistentVolumeClaimVolumeSource.ReadOnly: true` for read-only shared dirs (`knowledge-plane`) on a shared RWX Filestore PVC caused Linux NFSv3 / `kubelet` to mount the PV superblock `-o ro`, failing `workspace-provision` with `mkdir /workspace/.scion-provision.lock...: read-only file system`; omitting `ReadOnly` on the shared PVC source while preserving `VolumeMount.ReadOnly: true` keeps `/workspace` `rw` and `/scion-volumes/knowledge-plane` kernel-enforced `EROFS`.
  4. **GKE `per-agent` (`clone-per-agent`) workspace mode & patch-review handoff (`provision_factory.py`, `scion_factory.py`, `templates/_shared/agents.md`):**
     Upstream `k8s_runtime.go` refuses `workspaceMode: "shared"` when `gitRemote` is set. `provision_factory.py` defaults `kubernetes` projects to `per-agent`, pins `-b <branch>` on lead and child `scion start` commands, instructs specialists to exchange pre-commit diffs via `/scion-volumes/scratchpad/patches/<issue>.patch`, and pushes `git push origin HEAD:<branch>`.
  5. **Large-monorepo `tmux` readiness gate before `--go` (`provision_factory.py`):**
     Checking out ~130k files (`kubernetes/kubernetes`) onto `BASIC_HDD` Filestore inside `sciontool init` takes ~2–3 minutes *after* the K8s pod enters `phase: "running"`, before `sciontool init` spawns `/tmp/tmux-1000/default`. `provision_factory.py` now polls `exec_agent(..., ["tmux", "has-session", "-t", "scion"])` before sending `GO:`.
  6. **Firestore REST URL + K8s `CronJob` resilience (`factory_store.py`, `factory-watchdog.py`, `install-k8s-control-plane.py`):**
     `load_control_state` prepends `{store.api}/` to `store.doc_name(...)`, K8s `CronJob`s set `backoffLimit: 1` under `concurrencyPolicy: Forbid`, and API-mode `factory-watchdog.py` uses `should_wake` for `<prefix>-lead` monorepo slugs and camelCase `createdAt` on `/api/v1/messages`.
- **Encoded in:** [`scripts/setup-gke-hub.sh`](../scripts/setup-gke-hub.sh), [`scripts/hub_client.py`](../scripts/hub_client.py), [`config/harness/home/.local/bin/factory-cd.sh`](../config/harness/home/.local/bin/factory-cd.sh), [`scripts/provision_factory.py`](../scripts/provision_factory.py), [`scripts/factory_store.py`](../scripts/factory_store.py), [`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py), [`scripts/install-k8s-control-plane.py`](../scripts/install-k8s-control-plane.py), and [`docs/gke-setup-runbook.md`](gke-setup-runbook.md).

## L34. Git-backed Docker workspace concurrency
- **Symptom:** Running multiple specialist containers (`max_wip > 1`) on a Single-VM Docker Hub with `vcs.enabled: true` triggered `.git/index.lock` failures and concurrent `node_modules` / virtualenv corruption.
- **Root cause:** Defaulting `runtime: docker` projects to `workspaceMode: shared` bind-mounts a single host `/workspace` checkout into every agent container.
- **Fix:** Default `workspaceMode` to `per-agent` whenever Git VCS is enabled (`vcs.enabled: true`) or `runtime == "kubernetes"`, falling back to `shared` only for local non-Git projects.
- **Encoded in:** [`scripts/provision_factory.py`](../scripts/provision_factory.py) and [`scripts/topology_audit.py`](../scripts/topology_audit.py).

## L35. Workload-proportional role splitting vs. Single-VM container sizing
- **Symptom:** When >60% of backlog issues targeted one implementer or frontend role while `max_wip >= 3`, parallel capacity sat idle behind one container. Conversely, keeping 15 build and evaluation templates on a 4-vCPU Single-VM Docker host exhausted host memory.
- **Fix:** Warn during readiness checks (`audit-topology`) when one `owner_role` holds >60% of issues so the PO can split the role into parallel domain slices via `default_kind`, and warn when `runtime: docker` exceeds 10 active roles so idle defaults are pruned via `team.dropped_defaults` and evaluation probes are consolidated into `domain-evaluator`.
- **Encoded in:** [`scripts/topology_audit.py`](../scripts/topology_audit.py), [`scripts/scion_factory.py`](../scripts/scion_factory.py), and [`skills/scion-factory-bootstrap/SKILL.md`](../skills/scion-factory-bootstrap/SKILL.md).

## L36. Upstream skill curation, stack alignment, and role-scoped injection
- **Symptom:** Vendoring uncurated community skills injected 10k–25k lines of unrelated framework files into agent contexts and introduced conflicting package-manager commands (`pnpm` or `yarn` inside `npm` repositories).
- **Fix:** Audit skill banks with `./scripts/scion-factory audit-skills <dir> --spec factory.yaml` before compilation, cap skills at 3–6 focused packages per role, and inject role-scoped skills into `templates/<role>/skills/` via `--skill-bank`.
- **Encoded in:** [`scripts/skill_curation.py`](../scripts/skill_curation.py), [`scripts/scion_factory.py`](../scripts/scion_factory.py), and [`skills/scion-factory-bootstrap/SKILL.md`](../skills/scion-factory-bootstrap/SKILL.md).

## L37. Strict Scion container delegation and custom image UID invariants
- **Symptom:** Claude Code coordinators asked for fresh subagents defaulted to in-process `Agent` / `Task` tools instead of spawning isolated Scion child containers, and custom harness image layers ending with `USER scion` broke `sciontool init` host UID remapping (`Permission denied` on `/workspace/.git`).
- **Fix:** Support `team.strict_scion_delegation: true` to emit `templates/<role>/home/.claude/settings.json` denying `"Agent"` and `"Task"` on Claude leads, support `assign` service-account registration on Single-VM Docker hubs, and enforce the `USER root` + system browser checklist in `audit-topology`.
- **Encoded in:** [`scripts/skill_curation.py`](../scripts/skill_curation.py), [`scripts/provision_factory.py`](../scripts/provision_factory.py), [`scripts/topology_audit.py`](../scripts/topology_audit.py), and [`skills/scion-factory-bootstrap/SKILL.md`](../skills/scion-factory-bootstrap/SKILL.md).
