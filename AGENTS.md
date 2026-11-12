# `scion-defaults` - Canonical Agent & Operator Guidelines

This repository (`duboc/scion-defaults`) is the canonical blueprint, automation toolkit, and template catalog for deploying and operating an **always-on, self-healing multi-agent software factory on [Scion](https://github.com/GoogleCloudPlatform/scion)**.

## Repository Structure

| Path | Purpose |
|---|---|
| [`README.md`](README.md) | Overview, architecture, starting prompts, GKE and Single-VM setup, and replication reference |
| [`config/`](config/) | Parameterized environment (`factory.env.example`), the monorepo start-folder launcher (`harness/home/.local/bin/factory-cd.sh`), Hub `settings.yaml`, OpenCode Vertex AI model catalog (`opencode-models.json`), heartbeat prompt, and the Firestore control-plane contract, indexes, and rules (`firestore/`) |
| [`scripts/`](scripts/README.md) | Idempotent provisioning (`provision_factory.py`), GKE full-lifecycle CLI (`gke_lifecycle.py` for `gke-up` / `gke-down`), zero-LLM event-driven delta orchestrator (`factory_delta.py`), hillclimb runner (`run_hillclimb_wave.py`), admin API client (`scion-admin.py`), template rendering/pushing, telemetry setup, harness patching, 1-minute Firestore status sync (`sync-factory-agents.py` + `factory_store.py`), 5-minute self-healing watchdog, backups, and alerting |
| [`templates/`](templates/README.md) | Shared multi-agent coordination protocol (`_shared/agents.md`) + 10 role templates (`lead`, `backend`, `frontend`, `reviewer`, `deployer`, `eval-lead`, `factory-analyst`, `search-evaluator`, `ui-craftsman`, `eval-deployer`) |
| [`docs/`](docs/) | Architecture (`architecture.md`), setup runbook (`setup-runbook.md`), GKE setup runbook (`gke-setup-runbook.md`), GKE 3-run hillclimb report (`gke-hillclimb-report.md`), operations runbook (`operations-runbook.md`), agent protocol (`agent-protocol.md`), 5-layer evaluation pyramid (`verification-and-eval.md`), 34 lessons learned (`lessons-learned.md`), and v2 orchestration spec (`specs/`) |
| [`docs/telemetry/`](docs/telemetry/README.md) | ASD-STE100 documentation for reproducing telemetry, the evaluation data pipeline, and harness hooks / stacked Cloud Trace waterfalls |
| [`docs/grill-me-playbook.md`](docs/grill-me-playbook.md) | 5-pillar Socratic "Grill-Me" inquiry and domain-agnostic boilerplate compiler guide |
| [`skills/scion-factory-bootstrap/SKILL.md`](skills/scion-factory-bootstrap/SKILL.md) | Portable Agent Skill to interview users, challenge vague specs using L1 to L34, and compile new Scion factories |
| [`presets/`](presets/) | Reference `FactorySpec` presets (`starter-service.factory.yaml`, `psearch.factory.yaml`, `acme-airlines.factory.yaml`, `bananacast-gke.factory.yaml`) |
| [`patches/sciontool-stacked-traces/`](patches/sciontool-stacked-traces/README.md) | Source patch (`sciontool-stacked-traces.patch`) and `tracestate.go` for 3-level stacked Cloud Trace waterfalls and Cloud Monitoring `CUMULATIVE` epoch export |

## Security & Hygiene Rules (Mandatory)

1. **Zero Secrets or Real Project Identifiers in Tracked Files:**
   - Never commit PATs (`ghp_*`, `github_pat_*`, `scion_pat_*`), service account keys, emails, real GCP project IDs/numbers, or VM names.
   - All real environment values live exclusively in the git-ignored `config/factory.env`.
   - Before every commit, run:
     ```bash
     rg -i '(ghp_|github_pat_|scion_pat_|AIza|ya29\.|BEGIN PRIVATE KEY)' . --glob '!config/factory.env'
     ```
2. **Idempotent Scripts with `DRY_RUN=1`:**
   - Every provisioning and patching script in `scripts/` must support `DRY_RUN=1` (or `--dry-run`) and use `set -euo pipefail` + `CLOUDSDK_CORE_DISABLE_PROMPTS=1`.
3. **Serverless First, No Spanner in the Factory:**
   - The factory control plane (agent status, evaluation runs) uses Firestore through `scripts/factory_store.py`. Never add Spanner (or another provisioned database) to the factory itself; products may use one only when the interview records a requirement Firestore or BigQuery can't meet.
4. **Never Run `docker restart` on Scion Agent Containers:**
   - Scion creates agent containers with `--rm` (`AutoRemove: true`). Update binaries or credentials in running containers via `docker cp` ([`scripts/patch-sciontool-telemetry.sh`](scripts/patch-sciontool-telemetry.sh)).
