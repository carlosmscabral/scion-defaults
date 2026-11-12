# GKE Software Factory Hillclimb Report

Three unattended, full-lifecycle GKE runs (`scion-factory gke-up` -> backlog wave -> 30-minute idle soak -> `scion-factory gke-down --delete-project`) against `duboc/bananacast` on `antigravity-factory` (`Gemini 4 Argon`) completed with exit code `0`, zero manual edits, zero standing cloud resources, and untouched `main` and `factory/main` branches.

## Measured Scorecard

All real project IDs are anonymized (`scion-gke-hc<N>-<hex>`) per [`AGENTS.md`](../AGENTS.md). Every metric comes from the archived `scorecard.json`, pod `agent.log` files, and harness `transcript.jsonl` artifacts captured before project deletion.

| Metric | Prior Baselines (VM + `k8s-version`) | Run 1 (`scion-gke-hc1-<hex>`) | Run 2 (`scion-gke-hc2-<hex>`) | Run 3 (`scion-gke-hc3-<hex>`) |
|---|---|---|---|---|
| Cold-start `gke-up` total | 92m + manual edits | 48.9m (0 edits) | 47.5m (0 edits) | 48.7m (0 edits) |
| `setup-gke-hub.sh` / `provision` | manual steps | 42.3m / 6.6m | 41.5m / 6.0m | 43.0m / 5.7m |
| Delivered scope | manual | Wave 1 (3 issues) | Wave 2 (3 issues) | Wave 3 (3 issues + 2 drills) |
| Active wave time | n/a on ephemeral GKE | 35m | 24m | 35m (with mid-wave pod kill) |
| Pod Direct IAP `401` errors | 39k to 43k per pod | 0 | 0 | 0 |
| Pod heartbeat errors | repeated `401` | 0 | 0 | 0 |
| Cloud exporter warnings / errors | exporter inactive on GKE | 12 off-warnings / 0 errors | 0 / 0 (100% exported) | 0 / 0 (100% exported) |
| `lead` active turns per issue | ~8.5 | 2.0 (6 / 3) | 1.0 (3 / 3) | 2.5 (10 / 4, with drills) |
| Total steps / `USER_INPUT`s | hundreds per issue | 332 / 16 | 325 / 10 | 443 / 25 |
| Uncached input tokens | continuous burn | 1,454,899 | 657,278 (-54.8%) | 2,171,207 (4 issues + drills) |
| Cache-read / output tokens | n/a | 8,229,518 / 71,679 | 6,592,819 / 76,384 | 10,728,838 / 83,973 |
| 30m idle soak (pods / tokens) | ~490M cache tokens | 0 pods / 0 tokens | 0 pods / 0 tokens | 0 pods / 0 tokens |
| Post-run cloud resources | manual teardown | project deleted ($0) | project deleted ($0) | project deleted ($0) |

## What Changed Across Waves

### Cold-Start Debug Waves to Run 1 (`hc1`)

Bringing a brand-new Google Cloud project from `gcloud projects create` to a working Cloud Run Gen2 Direct IAP Hub + GKE Autopilot factory surfaced eight cold-start and permission blockers, all fixed in repository automation before Run 1:

1. **Pre-start Direct IAP token hook (`setup-gke-hub.sh`):** `sciontool init` starts heartbeats and status posts before `tmux` runs `factory-cd.sh`. Baking `/etc/scion/hooks/10-direct-iap-token` into `scion-base` mints `~/.scion/iap-access-token` before the harness launches, cutting pod IAP `401` errors from ~40k per pod to `0`.
2. **Single-architecture Cloud Build (`setup-gke-hub.sh`):** Narrowing `IMAGE_PLATFORMS` to `linux/amd64` on x86 Autopilot clusters removed QEMU `arm64` emulation and cut Hub + harness image builds from `92m` to `~41m`.
3. **Pinned Scion source and AST/brace shared-dir patch (`setup-gke-hub.sh`, `patch_scion_shared_dir_volume.py`):** Pinning `SCION_REF` (`6eb4b41`) and replacing a fragile regex with brace-balanced patching kept read-only `knowledge-plane` mounts kernel-enforced without mounting `/workspace` read-only on Filestore NFS.
4. **Compute API propagation, Vertex AI project env, and Hub RBAC (`setup-gke-hub.sh`, `provision_factory.py`):** Retrying `default` VPC creation across initial `SERVICE_DISABLED` windows, verifying project-scope `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` before `scion start`, and granting Hub project `project-owner` to `factory-control` allowed unattended `gke-up` and `factory_delta.py` pod management.

### Run 1 (`hc1`) to Run 2 (`hc2`)

Run 1 finished end to end without intervention, but transcript and log inspection showed three clear bottlenecks: `lead` woke on its own issue-label transitions (`6` active turns for `3` issues), pods logged `cloud exporter is not configured` (`12` lines), and Python test setup ran slowly over Filestore NFS. Fixing those three items before Run 2 cut wave execution from `35m` to `24m` and reduced uncached input tokens by `54.8%` (`1,454,899` -> `657,278`):

1. **Hub-level Cloud Telemetry and default KSA IAM (`setup-gke-hub.sh`, `provision_factory.py`, `setup-sandbox.sh`):** Upstream Scion strips template `telemetry:` blocks on Hub dispatch (`pkg/hub/dispatch_telemetry.go`) and starts `NewGCPExporter` on the pod's default Kubernetes ServiceAccount (`<hub>-agent@<project>`) before `metadataServer` activates `agent-worker`. Writing `telemetry.cloud` into Hub operational settings and granting logging, trace, and metric writer roles to `<hub>-agent@<project>` produced `100%` confirmed metric and log batches in Runs 2 and 3.
2. **In-flight delta echo suppression (`factory_delta.py`):** Recording `dispatched_issues` in Firestore state prevented `scion-factory-delta` from re-waking `lead` when `lead` or specialists moved already-dispatched issues across `status:ready`, `status:in-progress`, and `status:verified`. Active `lead` turns dropped to `3` (`1.0` per issue).
3. **Container-local `/tmp/.venv` (`role_library.py`, `scion_factory.py`):** Keeping `lead` out of Python environment setup and directing specialists and `reviewer` to container-local `/tmp/.venv` avoided Filestore NFS metadata stalls during `pytest`.

### Run 2 (`hc2`) to Run 3 (`hc3 --drills`)

Run 2 transcripts showed two edge cases that could delay drill recovery: `reviewer` called `sciontool status blocked` while waiting for writer patches, and an early CronJob tick called `/start` with `forceResume` while `lead` was still `starting`. Updating `factory_delta.py` (`f4f7b86`) treated `blocked` specialists as non-busy, skipped `/start` while `lead` is `provisioning` or `starting`, and added explicit `PO_REPLIES` closeout rules for zero-diff PO decisions.

## Run 3 Resilience Drill Results

1. **Mid-wave specialist pod eviction (`kubectl delete pod`):** While `#366` and `#368` were `status:in-progress`, `run_hillclimb_wave.py` deleted pod `bananacast-hc3--backend`. `sciontool` caught `SIGTERM` and updated the Hub agent record to `phase: "stopped"`. Once `#367` finished and zero specialists remained busy, `scion-factory-delta` woke `lead` with `stopped_children: ["backend"]`. `lead` restarted `backend`, and all three Wave 3 issues reached `status:verified` in `35m`.
2. **Product-owner decision sleep and wake (`status:po-decision`):** Seeding issue `#369` with `status:po-decision` kept the factory idle until `scion-factory-delta` stopped `lead` after the 10-minute grace window (`entered_sleep: true`, `0` running agent pods). Posting an unmarked human comment (`Approved Option A`) woke `lead` on the next CronJob tick in `112.6s` (within the `< 120s` target), and `lead` closed out `#369` as `status:verified` in a single turn.

## Reproducing a Full-Lifecycle Wave

Preview the full lifecycle without creating cloud resources:

```bash
python3 scripts/run_hillclimb_wave.py --wave 1 --env config/hc1.env --json
```

Run an unattended live wave (keep `--keep-on-failure` when debugging a new change, or `--drills` on Wave 3):

```bash
python3 scripts/run_hillclimb_wave.py --wave 3 --drills --env config/hc3.env --apply --json
```

For standalone ephemeral GKE factories outside the benchmark runner, use `./scripts/scion-factory gke-up` and `./scripts/scion-factory gke-down --delete-project` as documented in [`docs/gke-setup-runbook.md`](gke-setup-runbook.md).
