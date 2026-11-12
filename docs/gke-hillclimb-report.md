# GKE Software Factory Hillclimb Report

Four unattended, full-lifecycle GKE runs (`scion-factory gke-up` -> backlog wave -> 30-minute idle soak -> `scion-factory gke-down --delete-project`) against `duboc/bananacast` on `antigravity-factory` (`Gemini 4 Argon`) completed with exit code `0`, zero manual edits, zero standing cloud resources, and untouched `main` and `factory/main` branches.

## Measured Scorecard

All real project IDs are anonymized (`scion-gke-hc<N>-<hex>`) per [`AGENTS.md`](../AGENTS.md). Every metric comes from the archived `scorecard.json`, pod `agent.log` files, and harness `transcript_full.jsonl` artifacts captured before project deletion.

| Metric | Prior Baselines | Run 1 (`scion-gke-hc1-<hex>`) | Run 2 (`scion-gke-hc2-<hex>`) | Run 3 (`scion-gke-hc3-<hex>`) | Hardened Run (`scion-gke-hc3-<hex>`) |
|---|---|---|---|---|---|
| Cold-start `gke-up` total | 92m + manual edits | 48.9m (0 edits) | 47.5m (0 edits) | 48.7m (2 setup warnings) | 49.8m (0 warnings) |
| `setup-gke-hub.sh` / `provision` | manual steps | 42.3m / 6.6m | 41.5m / 6.0m | 43.0m / 5.7m | 43.1m / 6.7m |
| Delivered scope | manual | Wave 1 (3 issues) | Wave 2 (3 issues) | Wave 3 (3 issues + 2 drills) | Wave 3 (3 issues + 2 drills) |
| Active wave time | n/a on ephemeral GKE | 35m | 24m | 35m (pod kill) | 56m (pod kill) |
| Pass-1 Cloud Run probe crashes | Pass-1 `Error code 9` | 1 (recovered Pass 2) | 1 (recovered Pass 2) | 1 (recovered Pass 2) | 0 |
| Pod Direct IAP / heartbeat `401`s | 39k to 43k per pod | 0 / 0 | 0 / 0 | 0 / 0 | 0 for 60m; 14 at T+60m* |
| Cloud exporter warnings / errors | exporter inactive on GKE | 12 off-warnings / 0 | 0 / 0 (100% exported) | 0 / 0 (100% exported) | 0 / 0 (100% exported) |
| PO wake latency (`status:po-decision`) | manual wake | n/a | n/a | 112.6s | 70.0s |
| `lead` active turns per issue | ~8.5 | 2.0 (6 / 3) | 1.0 (3 / 3) | 2.5 (10 / 4) | 1.75 (7 / 4) |
| Total steps / `USER_INPUT`s | hundreds per issue | 332 / 16 | 325 / 10 | 443 / 25 | 392 / 21 |
| Uncached input tokens | continuous burn | 1,454,899 | 657,278 (-54.8%) | 2,171,207 | 2,260,095 |
| Cache-read / output tokens | n/a | 8,229,518 / 71,679 | 6,592,819 / 76,384 | 10,728,838 / 83,973 | 7,145,001 / 86,602 |
| 30m idle soak (pods / tokens) | ~490M cache tokens | 0 pods / 0 tokens | 0 pods / 0 tokens | 0 pods / 0 tokens | 0 pods / 0 tokens |
| Post-run cloud resources | manual teardown | project deleted ($0) | project deleted ($0) | project deleted ($0) | project deleted ($0) |

\*In the hardened Wave 3 run, `lead` stayed up for `67` minutes (`15:31Z` to `16:38Z`), longer than the `<50m` pod lifetimes in Runs 1 to 3. All heartbeats succeeded for the first `60` minutes (`0` errors), then `14` heartbeats returned `401` between `T+60m` and `T+67m` before idle shutdown. Root cause: `gke-metadata-server` caches Workload Identity OAuth2 access tokens until `5` minutes before expiry (`[T+55m, T+60m)`), so a `25`-minute refresh loop (`T+25m`, `T+50m`, `T+75m`) read the still-cached token at `T+50m` and slept past `T+60m`. Lowering `FACTORY_CD_IAP_REFRESH_SEC` from `1500` to `180` (`config/harness/home/.local/bin/factory-cd.sh`) polls inside `[T+55m, T+60m)` without rewriting unchanged token files.

## What Changed Across Waves

### Cold-Start Debug Waves to Run 1 (`hc1`)

Bringing a brand-new Google Cloud project from `gcloud projects create` to a working Cloud Run Gen2 Direct IAP Hub + GKE Autopilot factory surfaced eight cold-start and permission blockers, all fixed in repository automation before Run 1:

1. **Pre-start Direct IAP token hook (`make-factory-harness-configs.py`, `10-direct-iap-token`):** `sciontool init` starts heartbeats and status posts before `tmux` runs `factory-cd.sh`. Staging `~/.scion/hooks/pre-start.d/10-direct-iap-token` into `<harness>-factory` mints `~/.scion/transport-token` and `~/.scion/iap-access-token` during pre-start, cutting pod startup IAP `401` errors from ~40k per pod to `0`.
2. **Single-architecture Cloud Build (`setup-gke-hub.sh`):** Narrowing `IMAGE_PLATFORMS` to `linux/amd64` on x86 Autopilot clusters removed QEMU `arm64` emulation and cut Hub + harness image builds from `92m` to `~41m`.
3. **Pinned Scion source and AST/brace shared-dir patch (`setup-gke-hub.sh`, `patch_scion_shared_dir_volume.py`):** Pinning `SCION_REF` (`6eb4b41`) and replacing a fragile regex with brace-balanced patching kept read-only `knowledge-plane` mounts kernel-enforced without mounting `/workspace` read-only on Filestore NFS.
4. **Compute API propagation, Vertex AI project env, and Hub RBAC (`setup-gke-hub.sh`, `provision_factory.py`):** Retrying `default` VPC creation across initial `SERVICE_DISABLED` windows, verifying project-scope `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` before `scion start`, and granting Hub project `project-owner` to `factory-control` allowed unattended `gke-up` and `factory_delta.py` pod management.

### Run 1 (`hc1`) to Run 2 (`hc2`)

Run 1 finished end to end without intervention, but transcript and log inspection showed three bottlenecks: `lead` woke on its own issue-label transitions (`6` active turns for `3` issues), pods logged `cloud exporter is not configured` (`12` lines), and Python test setup ran slowly over Filestore NFS. Fixing those three items before Run 2 cut wave execution from `35m` to `24m` and reduced uncached input tokens by `54.8%` (`1,454,899` -> `657,278`):

1. **Hub-level Cloud Telemetry and default KSA IAM (`setup-gke-hub.sh`, `provision_factory.py`, `setup-sandbox.sh`):** Upstream Scion strips template `telemetry:` blocks on Hub dispatch (`pkg/hub/dispatch_telemetry.go`) and starts `NewGCPExporter` on the pod's default Kubernetes ServiceAccount (`<hub>-agent@<project>`) before `metadataServer` activates `agent-worker`. Writing `telemetry.cloud` into Hub operational settings and granting logging, trace, and metric writer roles to `<hub>-agent@<project>` produced `100%` confirmed metric and log batches in Runs 2 and 3.
2. **In-flight delta echo suppression (`factory_delta.py`):** Recording `dispatched_issues` in Firestore state prevented `scion-factory-delta` from re-waking `lead` when `lead` or specialists moved already-dispatched issues across `status:ready`, `status:in-progress`, and `status:verified`. Active `lead` turns dropped to `3` (`1.0` per issue).
3. **Container-local `/tmp/.venv` (`role_library.py`, `scion_factory.py`):** Keeping `lead` out of Python environment setup and directing specialists and `reviewer` to container-local `/tmp/.venv` avoided Filestore NFS metadata stalls during `pytest`.

### Run 2 (`hc2`) to Run 3 (`hc3 --drills`) and Post-Run-3 Hardening

Run 2 transcripts exposed blocked-specialist and startup races, fixed before Run 3 (`f4f7b86`) by treating `blocked` specialists as non-busy and skipping `/start` while `lead` is `provisioning` or `starting`. Inspecting Run 3's raw Cloud Run, `CronJob`, and transcript logs then uncovered five deeper bootstrap and control-plane issues, all fixed and verified in the hardened Wave 3 run (`587b4c4`..`a579ade`):

1. **Zero-crash Pass-1 Cloud Run Hub startup (`setup-gke-hub.sh`):** Upstream `settings.yaml.tftpl` omitted `oidc_audience` on Pass 1 (`iap_oauth_client_id == null`), causing `scion server` to exit with `Error: hosted HA deployment requires server.auth.transport.oidc_audience` (`Error code 9`). Rendering `oidc_audience: ${coalesce(iap_oauth_client_id, public_url)}`, extending `time_sleep.iam_propagation` to `180s`, and retrying Step-5 Hub API `PUT`s across Direct IAP propagation brought `setup_warnings` to `[]` with zero Pass-1 probe crashes.
2. **Built-in Hub `global` project exclusion (`hub-preflight.py`, `provision_factory.py`, `factory_delta.py`, `sync-factory-agents.py`, `factory-watchdog.py`):** Once Step-5 Hub env writes succeeded, Scion Hub materialized its built-in `"global"` project row in `/api/v1/projects`. Filtering out `slug == "global"` prevented `403` errors in K8s `CronJob`s.
3. **Go zero-time filtering, 1-minute delta cadence, and single-turn specialist launch (`factory_delta.py`, `install-k8s-control-plane.py`, `role_library.py`):** Rejecting Go zero timestamps (`"0001-01-01T00:00:00Z"`), starting `lead` + `tmux` before installing `scion-factory-delta` at `*/1 * * * *`, and passing brief paths directly to `scion start` cut Wave 3 `lead` active turns from `10` (`2.5`/issue) to `7` (`1.75`/issue) and dropped PO wake latency from `112.6s` to `70.0s`.
4. **Cloud Resource Manager `429`/`409` IAM backoff (`setup-sandbox.sh`, `setup-gke-hub.sh`):** Progressive `10s..70s` retry backoff absorbed per-minute IAM policy write bursts across fresh project setup.
5. **Sub-5-minute Direct IAP token refresh (`factory-cd.sh`, `10-direct-iap-token`):** Setting `FACTORY_CD_IAP_REFRESH_SEC=180` with unchanged-token deduplication ensures pods running longer than 60 minutes refresh `~/.scion/transport-token` inside `gke-metadata-server`'s 5-minute pre-expiry cache window.

## Resilience Drill Results

1. **Mid-wave specialist pod eviction (`kubectl delete pod`):** While Wave 3 issues were `status:in-progress`, `run_hillclimb_wave.py` deleted pod `bananacast-hc3--backend`. `sciontool` caught `SIGTERM` and updated the Hub agent record to `phase: "stopped"`. `scion-factory-delta` woke `lead` with `stopped_children: ["backend"]`, `lead` restarted `backend`, and all three Wave 3 issues reached `status:verified`.
2. **Product-owner decision sleep and wake (`status:po-decision`):** Seeding a `status:po-decision` issue kept the factory idle until `scion-factory-delta` stopped `lead` after the 10-minute grace window (`entered_sleep: true`, `0` running agent pods). Posting an unmarked human comment (`Approved Option A`) woke `lead` in `70.0s` (`112.6s` in Run 3, both under the `120s` target), and `lead` closed the issue as `status:verified` in a single turn.

## Reproducing a Full-Lifecycle Wave

Preview the full lifecycle without creating cloud resources:

```bash
python3 scripts/run_hillclimb_wave.py --wave 1 --env config/hc1.env --json
```

Run an unattended live wave (pass `--keep-on-failure` while iterating on a new change, or `--drills` on Wave 3):

```bash
python3 scripts/run_hillclimb_wave.py --wave 3 --drills --env config/hc3.env --apply --json
```

For standalone ephemeral GKE factories outside the benchmark runner, use `./scripts/scion-factory gke-up` and `./scripts/scion-factory gke-down --delete-project` as documented in [`docs/gke-setup-runbook.md`](gke-setup-runbook.md).
