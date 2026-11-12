# GKE Setup Runbook

This guide covers setting up Scion Hub on a Full GKE Multi-Hub HA architecture or a Hybrid VM+GKE target.

## Prerequisites & Environment

Before starting, configure your `config/factory.env` with the following GKE keys:
- `HUB_URL`: URL of the Scion Hub API (e.g. `https://<hub-name>-<project-number>.<region>.run.app`).
- `HUB_IAP_AUDIENCE`: IAP OAuth Client ID when IAP is enabled (`369001918367-<client-id>.apps.googleusercontent.com` on Cloud Run Direct IAP).
- `HUB_IMPERSONATE_SA`: Operator Google Service Account to impersonate from developer workstations when org policy blocks user-credential OIDC/IAP tokens (optional inside GKE pods, which use Workload Identity automatically).
- `HUB_TRANSPORT_MODE`: `iap` (default when `HUB_IAP_AUDIENCE` is set) or `serverless`.
- `AGENT_RUNTIME=kubernetes`: Instructs provisioning, sandbox IAM, and control-plane scripts to target GKE pods (`workspaceMode: "per-agent"` / `clone-per-agent`).
- `RUNTIME_PROFILE`: Set to `remote` on Full Terraform HA (`--hub-mode cloudrun`), empty `""` when using the Hub's default K8s profile, or `gke` for Hybrid VM+GKE.
- `HUB_K8S_CLUSTER` / `HUB_REGION`: GKE cluster name and region/zone.
- `HUB_K8S_NAMESPACE`: Kubernetes namespace for agent pods and control-plane `CronJob`s (e.g. `scion-hub`).
- `IMAGE_REGISTRY`: Artifact Registry path for Hub and harness images.
- `IMAGE_PLATFORMS`: Cloud Build target platforms (defaults to `linux/amd64` for x86 GKE Autopilot; set `linux/amd64,linux/arm64` only when running mixed-arch node pools).
- `SCION_REF`: Pinned upstream `GoogleCloudPlatform/scion` commit SHA cloned by `setup-gke-hub.sh` when `--scion-repo` does not exist yet (`--scion-ref <sha>` overrides).
- `WORKER_SA_NAME` / `CONTROL_SA_NAME`: GCP Service Accounts for agent pods (default `agent-worker`, KSA `scion-worker`) and K8s control-plane `CronJob`s (default `factory-control`, KSA `scion-factory-control`).
- `HUB_SA`: Hub runtime broker service account granted `roles/iam.serviceAccountUser` on worker/deployer SAs.

## One-Command Lifecycle (`gke-up` and `gke-down`)

To create an isolated Google Cloud project, bootstrap a Full GKE Multi-Hub HA Scion installation, compile `factory.yaml`, and provision the factory in one command, run `gke-up` in dry-run mode first and then pass `--apply`:

```bash
./scripts/scion-factory gke-up presets/bananacast-gke.factory.yaml \
  --env config/factory.env \
  --create-project \
  --billing-account "$BILLING_ACCOUNT_ID" \
  --folder "$FOLDER_ID"
./scripts/scion-factory gke-up presets/bananacast-gke.factory.yaml \
  --env config/factory.env \
  --create-project \
  --billing-account "$BILLING_ACCOUNT_ID" \
  --folder "$FOLDER_ID" \
  --apply
```

When a trial or benchmark run finishes, archive pod logs, harness transcripts, Firestore control state, and `scorecard.json` to `~/scion-backups/<project>-<timestamp>/` (`0700`) and delete the ephemeral project:

```bash
./scripts/scion-factory gke-down presets/bananacast-gke.factory.yaml \
  --env config/factory.env \
  --delete-project \
  --apply
```

`gke-down --delete-project` refuses non-`scion-gke-*` project IDs unless you pass `--confirm-project <id>`. See [`docs/gke-hillclimb-report.md`](gke-hillclimb-report.md) for measured 3-run benchmarks across cold-start time, token usage, idle soak, and resilience drills.

## Option A: Full GKE Multi-Hub HA Scion

For a completely serverless/managed infrastructure, use the Terraform configurations (`deploy/terraform/`: `configurations/shared-infra` + `configurations/hub`).

1. Plan the installation:
   ```bash
   DRY_RUN=1 scripts/setup-gke-hub.sh --tier terraform-ha --scion-repo <dir> --harnesses claude,antigravity --dry-run
   ```
2. Review the generated `.tfvars` and Terraform plan output.
3. Apply the installation:
   ```bash
   scripts/setup-gke-hub.sh --tier terraform-ha --scion-repo <dir> --harnesses claude,antigravity --approve-apply
   ```
4. Verify connectivity using IAP `Proxy-Authorization` and the `/health` endpoint. On Cloud Run Direct IAP (`369001918367-*`), `scripts/hub_client.py` (for K8s `CronJob`s), `/etc/scion/hooks/10-direct-iap-token` (pre-start pod hook), and `config/harness/home/.local/bin/factory-cd.sh` (in-pod refresh) automatically mint Workload Identity Service Account access tokens with `cloud-platform`, `userinfo.email`, and `openid` scopes, requiring zero static Kubernetes Secrets or PATs.

## Option B: Hybrid VM + GKE Tier

If you are using a single-VM Hub but targeting GKE for agent workloads (`gke_target`), you'll connect the VM to the cluster. Set `RUNTIME_PROFILE=gke` in your environment.

1. Plan the installation:
   ```bash
   DRY_RUN=1 scripts/setup-gke-hub.sh --tier hybrid-gke --scion-repo <dir> --harnesses claude,antigravity --dry-run
   ```
2. Apply the installation:
   ```bash
   scripts/setup-gke-hub.sh --tier hybrid-gke --scion-repo <dir> --harnesses claude,antigravity --approve-apply
   ```

## Multi-arch Artifact Registry Image Builds

Both topology options build multi-arch container images (`core-base`, `scion-base`, `claude`, `antigravity`, `opencode`) using Cloud Build and push them to your `IMAGE_REGISTRY` with immutable tags.

## Sandbox IAM & Workload Identity KSA Mapping

When setting up the environment, we map Google Service Accounts to Kubernetes Service Accounts (KSA). Use the sandbox setup script to configure this:

```bash
AGENT_RUNTIME=kubernetes scripts/setup-sandbox.sh
```

This binds Workload Identity to:
- `scion-worker` KSA for running agent tasks (default `agent-worker` GCP SA).
- `scion-factory-control` KSA for factory coordination.
- `scion-deployer` KSA for deploying the target service.

You can configure identity in `assign` vs `passthrough` modes depending on your security perimeter.

## Deterministic Factory Provisioning

Once the cluster and toolkit are ready, perform automated provisioning.

Review the plan:
```bash
./scripts/scion-factory provision factory.yaml --kit <dir> --env config/factory.env
```

Apply and start agents:
```bash
./scripts/scion-factory provision factory.yaml --kit <dir> --env config/factory.env --apply --go
```

## Control Plane Installation

To install the K8s CronJob control plane on the Kubernetes cluster, use the provided script:

1. Render and review manifests:
   ```bash
   python3 scripts/install-k8s-control-plane.py --namespace <ns> --dry-run --out manifests.yaml
   ```
2. Apply manifests:
   ```bash
   python3 scripts/install-k8s-control-plane.py --namespace <ns> --apply
   ```

This sets up `scion-factory-sync`, `scion-factory-watchdog`, and (when `team.heartbeat_mode: delta` is enabled) the 1-minute zero-LLM `scion-factory-delta` CronJob (`scripts/factory_delta.py`). Monitor them via:
```bash
kubectl get cronjobs,jobs,pods -n <ns>
```
Their state is stored persistently in the Firestore `factory_control_state` bookmark (`watchdog__default` and `delta__<slug>`).

### Event-Driven Delta Mode & Cloud Telemetry

- **Single-project delta mode (`team.heartbeat_mode: delta`, `project.eval_slug` omitted):** Instead of waking `lead` on a fixed 15-minute LLM schedule, `scion-factory-delta` (`scripts/factory_delta.py`) polls GitHub issues every minute with zero LLM tokens, wakes `lead` only on newly ready issues, unmarked human `status:po-decision` replies, or evicted specialist pods (`STALLED_WIP`), and stops all agent pods after 10 minutes idle (`$0` idle LLM burn).
- **Hub Cloud Telemetry (`telemetry.cloud`):** Upstream Scion strips per-template `telemetry:` blocks on Hub dispatch and starts `NewGCPExporter` on the pod's default KSA (`<hub-name>-agent@<hub-project>`). Both `setup-gke-hub.sh` and `provision_factory.py` configure `telemetry.cloud` via Hub operational settings (`settings.yaml` and `PUT /api/v1/admin/server-config`), and `setup-sandbox.sh` grants `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/monitoring.metricWriter` to `<hub-name>-agent@<hub-project>`.
