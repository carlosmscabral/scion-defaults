---
name: gke-hillclimb-verification
description: >-
  Use when running, debugging, or scoring full-lifecycle GKE Autopilot + Cloud Run
  Scion factory hillclimb waves (gke-up -> wave + drills + idle soak -> gke-down)
  to guarantee zero bootstrap warnings, lossless GCP telemetry, minimal token
  burn, and $0 lingering cloud resources.
---

# Full-Lifecycle GKE Hillclimb Verification

Never treat top-level `rc=0` alone as proof of a clean GKE run. Iterate until an unattended cold-start-to-teardown run (`gke-up --create-project` -> backlog wave + drills + 30m idle soak -> `gke-down --delete-project`) passes every infrastructure, telemetry, efficiency, and cleanup check.

## 1. Debug In Place With `--keep-on-failure`, Then Tear Down Immediately
- Run `scripts/run_hillclimb_wave.py --apply --keep-on-failure` while iterating so a failure preserves the live GKE cluster, Cloud Run Hub, and Cloud Logging long enough to inspect `kubectl logs -n <ns>`, `gcloud logging read` on Cloud Run revisions, and pod `~/agent.log` + `transcript_full.jsonl`.
- Immediately after capturing root cause, execute the printed `teardown_command` (`scion-factory gke-down ... --delete-project --apply`) and confirm `gcloud projects list --filter='projectId~^scion-gke-hc AND lifecycleState=ACTIVE'` is empty.

## 2. Enforce Zero-Warning Cold Start on Cloud Run + GKE Autopilot
Verify in `scorecard.json` (`gke_up`) and Cloud Run / Cloud Build logs:
- **`setup_warnings == []` and zero Pass-1 `Error code 9` probe crashes**: `settings.yaml.tftpl` must provide `oidc_audience: ${coalesce(iap_oauth_client_id, public_url)}` on Pass 1 before the Google-managed Direct IAP client (`369001918367-*`) is discovered, `time_sleep.iam_propagation` must allow conditioned Secret Manager IAM to settle (`180s`), and Step-5 Hub API `PUT`s must retry across Direct IAP IAM propagation.
- **Pre-start Direct IAP & lossless GCP telemetry**: `~/.scion/hooks/pre-start.d/10-direct-iap-token` (staged into `<harness>-factory` by `make-factory-harness-configs.py`) seeds `~/.scion/transport-token` and `~/.scion/iap-access-token` before `sciontool` heartbeats, `factory-cd.sh` polls every `180s` so pods living past 60 minutes catch `gke-metadata-server`'s 5-minute `[T+55m, T+60m)` token refresh window (`iap_401_errors == 0`), Hub operational settings configure `telemetry.cloud`, and `<hub-name>-agent@<hub-project>` holds `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/monitoring.metricWriter` (`telemetry_boot_errors == 0`, `metric_export_failures == 0`).

## 3. Enforce Zero-Echo Delta Orchestration & $0 Idle Soak
Verify across Kubernetes `CronJob` logs (`scion-factory-delta` at `*/1 * * * *`, `scion-factory-sync`, `scion-factory-watchdog`) and agent transcripts:
- **Exclude Hub's built-in `global` project**: `hub-preflight.py`, `provision_factory.py`, and all three `CronJob`s must filter `slug != "global"` from `FACTORY_PROJECTS` so Hub-scope env writes never cause `403` errors on `/api/v1/projects/global/agents`.
- **Start `lead` + `tmux` before installing `scion-factory-delta`**, ignore Go zero timestamps (`0001-01-01T00:00:00Z`), suppress in-flight label echoes while `not stalled_wip`, and launch fresh specialist pods with the brief pointer in `scion start` rather than `scion start` + duplicate `scion message`.
- **Resilience & scale-to-zero**: Confirm mid-wave `kubectl delete pod` specialist recovery (`STALLED_WIP`), `status:po-decision` 0-pod sleep + `< 2m` wake on unmarked human comments, `0` running agent pods and `0` tokens across the 30m idle soak, untouched `main` / `factory/main` SHAs, and `project_deleted: true`.
