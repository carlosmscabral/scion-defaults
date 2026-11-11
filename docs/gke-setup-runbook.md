# GKE Setup Runbook

This guide covers setting up Scion Hub on a Full GKE Multi-Hub HA architecture or a Hybrid VM+GKE target.

## Prerequisites & Environment

Before starting, configure your `config/factory.env` with the following GKE keys:
- `HUB_URL`: URL of the Scion Hub API (e.g. `https://<hub-name>-<project-number>.<region>.run.app`).
- `HUB_IAP_AUDIENCE`: IAP OAuth Client ID when IAP is enabled (`369001918367-<client-id>.apps.googleusercontent.com` on Cloud Run Direct IAP).
- `HUB_IMPERSONATE_SA`: Operator Google Service Account to impersonate from developer workstations when org policy blocks user-credential OIDC/IAP tokens (optional inside GKE pods, which use Workload Identity automatically).
- `HUB_TRANSPORT_MODE`: `iap` (default when `HUB_IAP_AUDIENCE` is set) or `serverless`.
- `AGENT_RUNTIME=kubernetes`: Instructs provisioning, sandbox IAM, and control-plane scripts to target GKE pods (`workspaceMode: "per-agent"` / `clone-per-agent`).
- `RUNTIME_PROFILE`: Set to `remote` on Full Terraform HA (`--hub-mode cloudrun`), `spot` to schedule worker pods on GKE Autopilot Spot nodes (configured via `setup-gke-hub.sh --spot-workers`), empty `""` when using the Hub's default K8s profile, or `gke` for Hybrid VM+GKE.
- `LEAD_PROFILE`: Optional profile override for `lead` and `eval-lead` pods. Defaults to `remote` (On-Demand) when `RUNTIME_PROFILE=spot` so factory orchestration stays resilient to Spot preemption.
- `HUB_K8S_CLUSTER` / `HUB_REGION`: GKE cluster name and region/zone.
- `HUB_K8S_NAMESPACE`: Kubernetes namespace for agent pods and control-plane `CronJob`s (e.g. `scion-hub`).
- `IMAGE_REGISTRY`: Artifact Registry path for multi-arch Hub and harness images.
- `WORKER_SA_NAME` / `CONTROL_SA_NAME`: GCP Service Accounts for agent pods (default `agent-worker`, KSA `scion-worker`) and K8s control-plane `CronJob`s (default `factory-control`, KSA `scion-factory-control`).
- `HUB_SA`: Hub runtime broker service account granted `roles/iam.serviceAccountUser` on worker/deployer SAs.

## Option A: Full GKE Multi-Hub HA Scion

For a completely serverless/managed infrastructure, use the Terraform configurations (`deploy/terraform/` — `configurations/shared-infra` + `configurations/hub`).

1. Plan the installation:
   ```bash
   DRY_RUN=1 scripts/setup-gke-hub.sh --tier terraform-ha --scion-repo <dir> --harnesses claude,antigravity --dry-run
   ```
2. Review the generated `.tfvars` and Terraform plan output.
3. Apply the installation:
   ```bash
   scripts/setup-gke-hub.sh --tier terraform-ha --scion-repo <dir> --harnesses claude,antigravity --approve-apply
   ```
   To run worker agents on GKE Autopilot Spot nodes while keeping `lead` and `eval-lead` on-demand, pass `--spot-workers` (or `SPOT_WORKERS=1`). This registers a `spot` profile on the Hub via `/api/v1/admin/server-config` and emits `RUNTIME_PROFILE=spot` and `LEAD_PROFILE=remote`. Because Spot pods lose uncommitted local workspace state if preempted, ensure workers commit and push frequently; `factory-watchdog.py` restarts failed workers and notifies `lead`.
4. Verify connectivity using IAP `Proxy-Authorization` and the `/health` endpoint. On Cloud Run Direct IAP (`369001918367-*`), both `scripts/hub_client.py` (for K8s `CronJob`s) and `config/harness/home/.local/bin/factory-cd.sh` (for agent pods) automatically mint Workload Identity Service Account access tokens with `cloud-platform`, `userinfo.email`, and `openid` scopes—requiring zero static Kubernetes Secrets or PATs.

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

This sets up `scion-factory-sync` and `scion-factory-watchdog` CronJobs. Monitor them via:
```bash
kubectl get cronjobs,jobs,pods -n <ns>
```
Their state is stored persistently in the Firestore `factory_control_state` bookmark.
