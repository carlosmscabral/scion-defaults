# Scripts

Every script reads `../config/factory.env` (copy it from `factory.env.example`; it is git-ignored). Override the path
with `FACTORY_ENV=/path/to/factory.env`. None of them prints or stores a secret: the Hub session secret is read on the
VM at run time, and the GitHub token is only ever piped through stdin.

| Script | Runs on | Changes things? | What it does |
|---|---|---|---|
| [`setup-gke-hub.sh`](setup-gke-hub.sh) | your machine | GCP & GKE | Setup script for a Full GKE HA Scion Hub or Hybrid VM+GKE architecture. |
| [`install-k8s-control-plane.py`](install-k8s-control-plane.py) | your machine | K8s Cluster | Installs `scion-factory-sync` and `scion-factory-watchdog` CronJobs into the Kubernetes cluster. |
| [`hub_client.py`](hub_client.py) | your machine | no | stdlib-only transport-agnostic Scion Hub REST client supporting IAP and GKE Autopilot `/health` cold starts. |
| [`scion-factory`](scion_factory.py) | your machine, called by a coding agent | writes `--out` | Deterministic tool behind the [`scion-factory-bootstrap`](../skills/scion-factory-bootstrap/SKILL.md) skill: `inspect` detects repository facts, `grill --json` writes a draft `factory.yaml` and emits the interview questions (never blocks on stdin), `check` runs the readiness gate, `compile` writes the factory kit (knowledge plane included), `provision` performs deterministic factory provisioning, and `enable-git` switches a factory that started without Git to Git mode. Knowledge plane logic lives in [`knowledge_plane.py`](knowledge_plane.py). |
| [`run-on-hub.sh`](run-on-hub.sh) | your machine | no (transport) | Streams a local script to the Hub VM over SSH stdin into a temporary file and runs it as the Hub OS user (`.py` with python3, anything else with bash), forwarding factory variables. Avoids nested quoting and argument length limits (L1, M7). |
| [`factory-status.sh`](factory-status.sh) | Hub VM | no | One-screen health check: agents with phase/activity/idle minutes, schedules, recent messages, the PO inbox, failed dispatches, workspace git state, containers, optional terminal tails (`--panes N`). |
| [`factory-watchdog.py`](factory-watchdog.py) | Hub VM (cron) | with `--apply` | Watchdog daemon running every 5 min: checks Antigravity provisioner drift, restores broken heartbeat schedules, wakes suspended agents, auto-restarts failed agents with a 3/hr circuit breaker, forwards `user:<email>` PO inbox escalations to Cloud Logging, and copies each factory's knowledge plane into the projects' read-only `knowledge-plane` shared directory (`KNOWLEDGE_PLANE_SOURCES` for several factories). |
| [`backup-hub.sh`](backup-hub.sh) | Hub VM (cron) | creates backups | Consistent SQLite online backup (`.backup`), tarball state archive of `~/.scion`, cron registration, and automated 14-day retention pruning. |
| [`setup-alerts-and-budgets.sh`](setup-alerts-and-budgets.sh) | your machine | GCP | Provisions Cloud Monitoring email alert channel, log-based alert policy for `scion-factory-alerts` (PO escalations and circuit breaker trips), and $150 monthly billing budgets on Hub and Sandbox projects with threshold alerts. |
| [`scion-admin.py`](scion-admin.py) | Hub VM | yes | PO/admin actions through the Hub API with a short-lived admin token: `projects`, `project-create` (git-backed project; `--token-file` for shared mode), `shared-dirs` (ensures `scratchpad` and read-only `knowledge-plane` in each factory project; dry run by default), `scion <args>` (runs the scion CLI on the VM as the PO, e.g. `scion start lead -t lead --project=<slug> "<task>"`), `message`, `schedule list/create/update/pause/resume`, `experiments`. |
| [`sync-factory-agents.py`](sync-factory-agents.py) | Hub VM (cron) | Firestore documents | Upserts live agent state from the Hub DB into Firestore `factory_agent_status` for the eval dashboard, and prunes deleted agents. `--dry-run` prints the requests. |
| [`factory_store.py`](factory_store.py) | Hub VM, eval agents, your machine | with `put-eval-run` | Standard-library Firestore REST client and CLI for the factory control plane: `list-agents`, `list-eval-runs`, `put-eval-run`. `--dry-run` prints the requests. |
| [`install-sync-cron.sh`](install-sync-cron.sh) | your machine | Hub VM crontab | Installs/updates the sync script, its `sync.env` and the every-minute cron line; runs one sync. `--dry-run` prints the remote script. |
| [`setup-telemetry.sh`](setup-telemetry.sh) | your machine | GCP & Hub VM | Provisions Cloud Logging, Cloud Monitoring, and Cloud Trace APIs, creates `scion-telemetry-writer` SA, grants telemetry viewer/writer roles, deploys credentials to VM and container homes, and updates Hub settings. Supports `DRY_RUN=1`. |
| [`patch-sciontool-telemetry.sh`](patch-sciontool-telemetry.sh) | your machine | Hub VM | Installs `/usr/local/bin/sciontool` wrapper in all running agent containers and base images (`scion-antigravity`, `scion-claude`, `scion-opencode`, `scion-hermes`) so PID 1 starts loopback OTLP listeners on 4317/4318 with GCP cloud exporters. |
| [`patch-antigravity-hooks.py`](patch-antigravity-hooks.py) | Hub VM | with `--apply` | Checks (default) or applies clean telemetry-enabled Antigravity hooks and Gemini 4 Argon model settings across provisioners, `.agents/hooks.json`, pre-start scripts, and Hub DB project hooks (L6). |
| [`setup-sandbox.sh`](setup-sandbox.sh) | your machine | GCP | Idempotent sandbox setup: APIs, deployer and runtime service accounts, IAM, Firestore (database + control-plane indexes), optional BigQuery, optional product-only Spanner, bucket, `github-token` secret. `DRY_RUN=1` prints the plan. |
| [`render-templates.py`](render-templates.py) | your machine | writes `build/` | Renders `--src <kit>/templates` with your values into `build/templates/<role>/`; `--file` renders one file to stdout (used for the heartbeat text); `--agent-names` prints `<role>\t<agent name>` (`AGENT_PREFIX` makes names `<prefix>-<role>` for several factories on one Hub). |
| [`push-templates.py`](push-templates.py) | your machine | with `--apply` | Renders roles from `--templates <kit>/templates` and creates or updates them as Hub templates through the API (dry run by default). Never uploads into a storage path a template doesn't own; renames such rows to `<role>-legacy` (L16). |
| [`hub-preflight.py`](hub-preflight.py) | Hub VM | no | Read-only check of an existing Hub before you add a factory: health, the PO's admin user, harness images and configs, existing projects, and the merged `FACTORY_PROJECTS` that keeps other factories covered by the watchdog and the sync. Exits 1 on blocking problems. |
| [`make-factory-harness-configs.py`](make-factory-harness-configs.py) | Hub VM | with `--apply` | Builds and publishes `claude-factory`, `antigravity-factory`, `opencode-factory`, `hermes-factory`: copies of the bundled harness-configs whose command starts with [`factory-cd.sh`](../config/harness/home/.local/bin/factory-cd.sh), so agents of a monorepo factory start in `FACTORY_SUBDIR`. Never edits the bundled configs; re-run after a Scion upgrade. |

## Common commands

```bash
cd docs/scion
cp config/factory.env.example config/factory.env && $EDITOR config/factory.env

scripts/run-on-hub.sh scripts/factory-status.sh --since 120            # what is the factory doing?
scripts/run-on-hub.sh scripts/factory-status.sh --panes 20             # ...including each agent's terminal
scripts/run-on-hub.sh scripts/scion-admin.py message --project psearch --agent lead \
  --text "PO -> lead: <decision or instruction>"
scripts/run-on-hub.sh scripts/scion-admin.py schedule list --project psearch
scripts/run-on-hub.sh scripts/scion-admin.py schedule update --project psearch --name psearch-lead-heartbeat \
  --text "$(scripts/render-templates.py --file config/heartbeat-message.txt)"
scripts/run-on-hub.sh scripts/patch-antigravity-hooks.py                # check; add --apply to fix
scripts/push-templates.py --project psearch-eval --templates <kit>/templates --roles eval-lead # dry run; add --apply to push
DRY_RUN=1 scripts/setup-sandbox.sh --with-bigquery
```

## Conventions

- Bash scripts use `set -euo pipefail`; Python scripts use only the standard library and call external commands with
  argument lists (no shell).
- Scripts that change things are idempotent and have a check or dry-run mode. Run that first.
- Scripts that run on the VM open the Hub DB read-only (`?mode=ro`). Writes go through the Hub API, never into the DB:
  hand-edited rows are how two templates ended up sharing one template's files (L16).
