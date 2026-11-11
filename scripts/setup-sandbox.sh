#!/usr/bin/env bash
# Idempotent setup of the GCP sandbox project the factory builds and deploys into. Runs on YOUR machine, with
# gcloud authenticated as an owner of SANDBOX_PROJECT who can also read the Hub VM in HUB_PROJECT.
#
#   DRY_RUN=1 scripts/setup-sandbox.sh --with-bigquery   # print the plan, change nothing
#   scripts/setup-sandbox.sh --with-bigquery
#   read -rs GITHUB_TOKEN; export GITHUB_TOKEN; scripts/setup-sandbox.sh   # also store the token (stdin, not argv)
#
# What it does (each step is skipped when already in place):
#   1. enables the APIs (explicitly, so gcloud never stops at an interactive "enable API?" prompt; L13)
#   2. creates the deployer SA (DEPLOYER_SA_NAME): the identity Scion assigns to the deployer agents
#   3. creates the runtime SA (RUNTIME_SA_NAME): the product's Cloud Run identity. Never run the product on the
#      default compute SA: it got Vertex AI 403s (L7). The deployer can act as it.
#   4. lets the Hub VM's SA mint tokens for the deployer SA (Scion "assign" identity mode), and gives it what the
#      Hub-side sync and the eval agents (passthrough identity) need: Firestore, the data bucket, and
#      serviceUsageConsumer so it can bill API calls to the sandbox (L12)
#   5. lets the default compute SA run Cloud Build for `gcloud run deploy --source` (needed when the organization
#      disables the automatic Editor grant)
#   6. --with-spanner (opt-in, product only): instance (100 processing units) and database. The factory itself
#      never needs Spanner; use it only when the product has a requirement that Firestore or BigQuery can't meet.
#   7. --with-bigquery: the dataset
#   8. Firestore database (FIRESTORE_DATABASE, default `(default)`) in Native mode with the factory control-plane
#      indexes (config/firestore/firestore.indexes.json), and the data bucket (uniform access, public access
#      prevented)
#   9. if GITHUB_TOKEN is set: secret `github-token` (read from stdin). An existing secret is left alone unless
#      UPDATE_SECRET=1.
# The dashboard's runtime SA (DASHBOARD_RUNTIME_SA, default: the project's default compute SA) gets read access to
# Firestore (roles/datastore.viewer) and the secret. A dedicated, read-only SA is better; create one and set DASHBOARD_RUNTIME_SA.
#
# Nothing to deploy (infrastructure.deploy: none; the kit's factory.env.example has no DEPLOYER_SA_NAME or
# RUNTIME_SA_NAME): steps 2-3, the deployer grants of step 4, and step 5 are skipped. Setting only one of the two is
# an error.
#
# The roles are deliberately broad for a throwaway sandbox (see architecture.md, Identities). Tighten them for
# anything shared.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${FACTORY_ENV:-$here/../config/factory.env}"
[[ -f "$env_file" ]] || { echo "missing $env_file (copy config/factory.env.example)" >&2; exit 2; }
# Keep a GITHUB_TOKEN exported by the caller even if factory.env doesn't define one.
caller_token="${GITHUB_TOKEN:-}"
set -a
# shellcheck source=/dev/null
. "$env_file"
set +a
GITHUB_TOKEN="${caller_token:-${GITHUB_TOKEN:-}}"

with_spanner=0
with_bigquery=0
for arg in "$@"; do
  case "$arg" in
    --with-spanner) with_spanner=1 ;;
    --with-bigquery) with_bigquery=1 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

P="$SANDBOX_PROJECT"
R="$SANDBOX_REGION"
# A factory with nothing to deploy has neither SA name (spec section 8): no deploy identities to set up.
DEPLOYER_SA_NAME="${DEPLOYER_SA_NAME:-}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-}"
if [[ -z "$DEPLOYER_SA_NAME" && -z "$RUNTIME_SA_NAME" ]]; then
  deploys=0
  DEPLOYER_SA="none"
  RUNTIME_SA="none"
elif [[ -z "$DEPLOYER_SA_NAME" || -z "$RUNTIME_SA_NAME" ]]; then
  echo "set both DEPLOYER_SA_NAME and RUNTIME_SA_NAME (a factory that deploys a service), or neither (nothing to deploy)" >&2
  exit 2
else
  deploys=1
  DEPLOYER_SA="$DEPLOYER_SA_NAME@$P.iam.gserviceaccount.com"
  RUNTIME_SA="$RUNTIME_SA_NAME@$P.iam.gserviceaccount.com"
fi
DRY_RUN="${DRY_RUN:-0}"
# Never stop at an interactive prompt (e.g. "API not enabled, enable and retry?"): it hangs automation (L13).
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %s\n' "$*" >&2   # stderr, so plans stay visible where callers silence stdout
  else
    "$@"
  fi
}
exists() { "$@" >/dev/null 2>&1; }   # read-only describe calls; they also run in DRY_RUN mode
step() { printf '\n== %s\n' "$*"; }
# A service account created a moment ago can be missing from IAM for up to a minute ("does not exist"), so IAM
# bindings retry instead of failing the run (L28).
iam() {
  if [[ "$DRY_RUN" == "1" ]]; then run "$@"; return; fi
  local attempt err
  for attempt in 1 2 3 4 5 6; do
    if err="$("$@" 2>&1 >/dev/null)"; then return 0; fi
    grep -q 'does not exist' <<< "$err" && [[ "$attempt" -lt 6 ]] || { echo "$err" >&2; return 1; }
    echo "  IAM has not seen a new service account yet; retrying in 10s ($attempt/5)" >&2
    sleep 10
  done
}
bind_project() { iam gcloud projects add-iam-policy-binding "$P" --member="$1" --role="$2" --condition=None --quiet; }

AGENT_RUNTIME="${AGENT_RUNTIME:-}"
HUB_K8S_NAMESPACE="${HUB_K8S_NAMESPACE:-}"
IS_GKE_MODE=0
if [[ "$AGENT_RUNTIME" == "kubernetes" || -n "$HUB_K8S_NAMESPACE" ]]; then
  IS_GKE_MODE=1
fi

HUB_VM="${HUB_VM:-}"
HUB_SA="${HUB_SA:-}"

if [[ "$IS_GKE_MODE" == 1 ]]; then
  if [[ -n "$HUB_SA" && "$HUB_SA" != *"@"* ]]; then
    HUB_SA="${HUB_SA}@${HUB_PROJECT}.iam.gserviceaccount.com"
  fi
  if [[ -n "$HUB_VM" && -z "$HUB_SA" ]]; then
    HUB_SA="$(gcloud compute instances describe "$HUB_VM" --project="$HUB_PROJECT" --zone="$HUB_ZONE" \
      --format='value(serviceAccounts[0].email)' 2>/dev/null || true)"
  elif [[ -z "$HUB_SA" && -n "$HUB_K8S_NAMESPACE" && -n "$HUB_PROJECT" ]]; then
    _cand_sa="${HUB_K8S_NAMESPACE}-hub@${HUB_PROJECT}.iam.gserviceaccount.com"
    if exists gcloud iam service-accounts describe "$_cand_sa" --project="$HUB_PROJECT"; then
      HUB_SA="$_cand_sa"
    fi
  fi
  HUB_VM_SA="$HUB_SA"
else
  HUB_VM_SA="${HUB_VM_SA:-$HUB_SA}"
  if [[ -z "$HUB_VM_SA" ]]; then
    HUB_VM_SA="$(gcloud compute instances describe "$HUB_VM" --project="$HUB_PROJECT" --zone="$HUB_ZONE" \
      --format='value(serviceAccounts[0].email)' 2>/dev/null || true)"
  fi
  if [[ -z "$HUB_VM_SA" ]]; then
    [[ "$DRY_RUN" == "1" ]] || { echo "cannot read the Hub VM's service account; set HUB_VM_SA" >&2; exit 1; }
    HUB_VM_SA="<hub-vm-service-account>"
  fi
fi

PROJECT_NUMBER="$(gcloud projects describe "$P" --format='value(projectNumber)' 2>/dev/null || echo '<project-number>')"
COMPUTE_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
DASHBOARD_RUNTIME_SA="${DASHBOARD_RUNTIME_SA:-$COMPUTE_SA}"
FS_DATABASE="${FIRESTORE_DATABASE:-(default)}"
echo "sandbox=$P region=$R deployer=$DEPLOYER_SA runtime=$RUNTIME_SA hub_vm_sa=$HUB_VM_SA dry_run=$DRY_RUN gke=$IS_GKE_MODE"

step "1. APIs"
apis=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com
      iam.googleapis.com iamcredentials.googleapis.com secretmanager.googleapis.com firestore.googleapis.com
      storage.googleapis.com logging.googleapis.com cloudtrace.googleapis.com monitoring.googleapis.com
      serviceusage.googleapis.com)
[[ "$with_spanner" == 1 ]] && apis+=(spanner.googleapis.com)
[[ "$with_bigquery" == 1 ]] && apis+=(bigquery.googleapis.com)
run gcloud services enable "${apis[@]}" --project="$P"

if [[ "$deploys" == 0 ]]; then
  echo "nothing to deploy (no DEPLOYER_SA_NAME / RUNTIME_SA_NAME): skipping steps 2-3 (deployer and runtime SAs and their roles), the Hub VM grants on the deployer SA (step 4) and step 5 (Cloud Build identity)"
fi

if [[ "$deploys" == 1 ]]; then
  step "2-3. service accounts"
  for sa in "$DEPLOYER_SA_NAME:Scion deployer agents" "$RUNTIME_SA_NAME:Cloud Run runtime of the product"; do
    name="${sa%%:*}"
    if exists gcloud iam service-accounts describe "$name@$P.iam.gserviceaccount.com" --project="$P"; then
      echo "$name: exists"
    else
      run gcloud iam service-accounts create "$name" --project="$P" --display-name="${sa#*:}"
    fi
  done

  deployer_roles=(roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.admin roles/storage.admin
                  roles/logging.viewer roles/serviceusage.serviceUsageConsumer roles/aiplatform.user
                  roles/iam.serviceAccountUser roles/datastore.user)
  [[ "$with_spanner" == 1 ]] && deployer_roles+=(roles/spanner.admin roles/spanner.databaseAdmin)
  [[ "$with_bigquery" == 1 ]] && deployer_roles+=(roles/bigquery.admin)
  for role in "${deployer_roles[@]}"; do bind_project "serviceAccount:$DEPLOYER_SA" "$role"; done
  echo "deployer project roles: ${deployer_roles[*]}"

  bind_project "serviceAccount:$RUNTIME_SA" roles/aiplatform.user
  iam gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --project="$P" \
    --member="serviceAccount:$DEPLOYER_SA" --role=roles/iam.serviceAccountUser --quiet
  echo "runtime SA: roles/aiplatform.user; deployer can act as it"
fi

step "4. Hub VM service account"
if [[ -n "$HUB_VM_SA" ]]; then
  if [[ "$deploys" == 1 ]]; then
    for role in roles/iam.serviceAccountTokenCreator roles/iam.serviceAccountUser; do
      iam gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --project="$P" \
        --member="serviceAccount:$HUB_VM_SA" --role="$role" --quiet
    done
  fi
  for role in roles/serviceusage.serviceUsageConsumer roles/datastore.user; do
    bind_project "serviceAccount:$HUB_VM_SA" "$role"
  done
  if [[ "$deploys" == 1 ]]; then
    echo "VM SA: token creator + user on the deployer SA; serviceUsageConsumer + datastore.user on $P"
  else
    echo "VM SA: serviceUsageConsumer + datastore.user on $P"
  fi
else
  echo "VM SA: skipped (GKE Mode without HUB_SA)"
fi

if [[ "$deploys" == 1 ]]; then
  step "5. Cloud Build identity for source deploys"
  bind_project "serviceAccount:$COMPUTE_SA" roles/cloudbuild.builds.builder
  echo "default compute SA: cloudbuild.builds.builder"
fi

if [[ "$with_spanner" == 1 ]]; then
  step "6. Spanner"
  if exists gcloud spanner instances describe "$SPANNER_INSTANCE" --project="$P"; then
    echo "instance $SPANNER_INSTANCE: exists"
  else
    run gcloud spanner instances create "$SPANNER_INSTANCE" --project="$P" --config="regional-$R" \
      --processing-units=100 --description="factory sandbox"
  fi
  if exists gcloud spanner databases describe "$SPANNER_DATABASE" --instance="$SPANNER_INSTANCE" --project="$P"; then
    echo "database $SPANNER_DATABASE: exists"
  else
    run gcloud spanner databases create "$SPANNER_DATABASE" --instance="$SPANNER_INSTANCE" --project="$P"
  fi
  if [[ "$deploys" == 1 ]]; then
    iam gcloud spanner databases add-iam-policy-binding "$SPANNER_DATABASE" --instance="$SPANNER_INSTANCE" \
      --project="$P" --member="serviceAccount:$RUNTIME_SA" --role=roles/spanner.databaseUser --quiet
    echo "databaseUser on $SPANNER_DATABASE: runtime SA (product only; the factory control plane is on Firestore)"
  fi
fi

if [[ "$with_bigquery" == 1 ]]; then
  step "7. BigQuery"
  if exists bq --project_id="$P" show "$BQ_DATASET"; then
    echo "dataset $BQ_DATASET: exists"
  else
    run bq --project_id="$P" mk --dataset --location="${BQ_LOCATION:-$R}" "$P:$BQ_DATASET"
  fi
fi

step "8. Firestore (factory control plane) and data bucket"
if exists gcloud firestore databases describe --database="$FS_DATABASE" --project="$P"; then
  echo "firestore $FS_DATABASE: exists"
else
  run gcloud firestore databases create --database="$FS_DATABASE" --location="$R" --type=firestore-native --project="$P"
fi
existing_indexes="$(gcloud firestore indexes composite list --project="$P" --database="$FS_DATABASE" \
  --format='value(name)' 2>/dev/null || true)"
create_index() {
  local group="$1"; shift
  if grep -q "collectionGroups/$group/" <<< "$existing_indexes"; then
    echo "index on $group: exists"
  else
    if [[ "$DRY_RUN" == "1" ]]; then
      run gcloud firestore indexes composite create --project="$P" --database="$FS_DATABASE" \
        --collection-group="$group" "$@" --async --quiet
    elif ! err="$(gcloud firestore indexes composite create --project="$P" --database="$FS_DATABASE" \
        --collection-group="$group" "$@" --async --quiet 2>&1 >/dev/null)"; then
      # Another run (another factory on the same sandbox) can create it between the list and the create.
      grep -q 'ALREADY_EXISTS' <<< "$err" || { echo "$err" >&2; return 1; }
      echo "index on $group: exists"
      return 0
    fi
    echo "index on $group: creating (async)"
  fi
}
# Keep in sync with config/firestore/firestore.indexes.json (tests check that file).
create_index factory_agent_status --field-config=field-path=project_slug,order=ascending \
  --field-config=field-path=agent_name,order=ascending
create_index eval_runs --field-config=field-path=project_slug,order=ascending \
  --field-config=field-path=evaluated_at,order=descending
bind_project "serviceAccount:$DASHBOARD_RUNTIME_SA" roles/datastore.viewer
echo "datastore.viewer: $DASHBOARD_RUNTIME_SA (dashboard); datastore.user: VM SA (sync, eval agents)"
if exists gcloud storage buckets describe "gs://$DATA_BUCKET" --project="$P"; then
  echo "bucket gs://$DATA_BUCKET: exists"
else
  run gcloud storage buckets create "gs://$DATA_BUCKET" --project="$P" --location="$R" \
    --uniform-bucket-level-access --public-access-prevention
fi
if [[ -n "$HUB_VM_SA" ]]; then
  iam gcloud storage buckets add-iam-policy-binding "gs://$DATA_BUCKET" --member="serviceAccount:$HUB_VM_SA" \
    --role=roles/storage.objectAdmin --quiet
  echo "VM SA: storage.objectAdmin on gs://$DATA_BUCKET"
fi

step "9. GitHub token secret"
if [[ -z "$GITHUB_TOKEN" ]]; then
  echo "GITHUB_TOKEN not set: skipped (export it to store it as secret github-token)"
else
  secret_exists=0
  exists gcloud secrets describe github-token --project="$P" && secret_exists=1
  if [[ "$secret_exists" == 0 ]]; then
    run gcloud secrets create github-token --project="$P" --replication-policy=automatic
  fi
  if [[ "$secret_exists" == 0 || "${UPDATE_SECRET:-0}" == 1 ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "+ printf '%s' \"\$GITHUB_TOKEN\" | gcloud secrets versions add github-token --project=$P --data-file=-"
    else
      printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add github-token --project="$P" --data-file=- >/dev/null
      echo "github-token: new version added"
    fi
  else
    echo "github-token: exists (UPDATE_SECRET=1 adds a new version)"
  fi
  iam gcloud secrets add-iam-policy-binding github-token --project="$P" \
    --member="serviceAccount:$DASHBOARD_RUNTIME_SA" --role=roles/secretmanager.secretAccessor --quiet
  echo "secretAccessor on github-token: $DASHBOARD_RUNTIME_SA"
fi

if [[ "$IS_GKE_MODE" == 1 ]]; then
  step "10. GKE Workload Identity"
  WORKER_SA_NAME="${WORKER_SA_NAME:-agent-worker}"
  WORKER_SA="${WORKER_SA_NAME}@${P}.iam.gserviceaccount.com"
  CONTROL_SA_NAME="${CONTROL_SA_NAME:-factory-control}"
  CONTROL_SA="${CONTROL_SA_NAME}@${P}.iam.gserviceaccount.com"

  if exists gcloud iam service-accounts describe "$WORKER_SA" --project="$P"; then
    echo "service-account $WORKER_SA_NAME: exists"
  else
    run gcloud iam service-accounts create "$WORKER_SA_NAME" --project="$P" \
      --description="Identity for Scion agents running in GKE" --display-name="Agent Worker"
  fi
  for role in roles/aiplatform.user roles/datastore.user roles/serviceusage.serviceUsageConsumer roles/logging.logWriter roles/cloudtrace.agent roles/monitoring.metricWriter; do
    bind_project "serviceAccount:$WORKER_SA" "$role"
  done
  if [[ -n "${HUB_K8S_NAMESPACE:-}" && -n "${HUB_PROJECT:-}" ]]; then
    # sciontool PID 1 initializes GCPExporter before the metadata server starts, authenticating via the
    # pod KSA's Workload Identity GSA (<hub_name>-agent@<hub_project>) rather than WORKER_SA.
    _hub_agent_sa="${HUB_K8S_NAMESPACE}-agent@${HUB_PROJECT}.iam.gserviceaccount.com"
    if [[ "$_hub_agent_sa" != "$WORKER_SA" ]] && exists gcloud iam service-accounts describe "$_hub_agent_sa" --project="$HUB_PROJECT"; then
      for role in roles/logging.logWriter roles/cloudtrace.agent roles/monitoring.metricWriter; do
        bind_project "serviceAccount:$_hub_agent_sa" "$role"
      done
    fi
  fi
  if [[ -n "${DATA_BUCKET:-}" ]]; then
    iam gcloud storage buckets add-iam-policy-binding "gs://$DATA_BUCKET" --member="serviceAccount:$WORKER_SA" \
      --role=roles/storage.objectAdmin --quiet
  fi

  if [[ "$CONTROL_SA" != "$WORKER_SA" ]]; then
    if exists gcloud iam service-accounts describe "$CONTROL_SA" --project="$P"; then
      echo "service-account $CONTROL_SA_NAME: exists"
    else
      run gcloud iam service-accounts create "$CONTROL_SA_NAME" --project="$P" \
        --description="Identity for Scion factory control CronJobs in GKE" --display-name="Factory Control"
    fi
    for role in roles/datastore.user roles/serviceusage.serviceUsageConsumer; do
      bind_project "serviceAccount:$CONTROL_SA" "$role"
    done
    if [[ -n "${DATA_BUCKET:-}" ]]; then
      iam gcloud storage buckets add-iam-policy-binding "gs://$DATA_BUCKET" --member="serviceAccount:$CONTROL_SA" \
        --role=roles/storage.objectAdmin --quiet
    fi
  fi

  if [[ -n "$HUB_VM_SA" ]]; then
    for role in roles/iam.serviceAccountTokenCreator roles/iam.serviceAccountUser; do
      iam gcloud iam service-accounts add-iam-policy-binding "$WORKER_SA" --project="$P" \
        --member="serviceAccount:$HUB_VM_SA" --role="$role" --quiet
    done
    echo "Hub SA ($HUB_VM_SA): token creator + user on $WORKER_SA"
  fi

  HUB_SVC="${HUB_CLOUDRUN_SERVICE:-${HUB_NAME:-}}"
  HUB_R="${HUB_REGION:-}"
  if [[ -z "$HUB_SVC" && "${HUB_URL:-}" =~ ^https://([a-z0-9-]+)-[0-9]+\.([a-z0-9-]+)\.run\.app ]]; then
    HUB_SVC="${BASH_REMATCH[1]}"
    HUB_R="${HUB_R:-${BASH_REMATCH[2]}}"
  fi
  HUB_R="${HUB_R:-$R}"
  if [[ -n "${HUB_PROJECT:-}" && -n "$HUB_SVC" ]]; then
    iam gcloud iap web add-iam-policy-binding --project="$HUB_PROJECT" --region="$HUB_R" \
      --resource-type=cloud-run --service="$HUB_SVC" \
      --member="serviceAccount:$WORKER_SA" --role=roles/iap.httpsResourceAccessor --quiet
    if [[ "$CONTROL_SA" != "$WORKER_SA" ]]; then
      iam gcloud iap web add-iam-policy-binding --project="$HUB_PROJECT" --region="$HUB_R" \
        --resource-type=cloud-run --service="$HUB_SVC" \
        --member="serviceAccount:$CONTROL_SA" --role=roles/iap.httpsResourceAccessor --quiet
    fi
    echo "Cloud Run Direct IAP ($HUB_SVC): httpsResourceAccessor granted to $WORKER_SA and $CONTROL_SA"
  fi

  if [[ -n "${HUB_PROJECT:-}" && -n "${HUB_K8S_NAMESPACE:-}" ]]; then
    iam gcloud iam service-accounts add-iam-policy-binding "$WORKER_SA" --project="$P" \
      --role=roles/iam.workloadIdentityUser \
      --member="serviceAccount:${HUB_PROJECT}.svc.id.goog[${HUB_K8S_NAMESPACE}/scion-worker]" --quiet
    iam gcloud iam service-accounts add-iam-policy-binding "$CONTROL_SA" --project="$P" \
      --role=roles/iam.workloadIdentityUser \
      --member="serviceAccount:${HUB_PROJECT}.svc.id.goog[${HUB_K8S_NAMESPACE}/scion-factory-control]" --quiet
    if [[ "$deploys" == 1 ]]; then
      iam gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --project="$P" \
        --role=roles/iam.workloadIdentityUser \
        --member="serviceAccount:${HUB_PROJECT}.svc.id.goog[${HUB_K8S_NAMESPACE}/scion-deployer]" --quiet
    fi
    if command -v kubectl >/dev/null 2>&1 || [[ "$DRY_RUN" == "1" ]]; then
      ensure_ksa() {
        local ksa="$1" gsa="$2"
        if [[ "$DRY_RUN" == "1" ]]; then
          run kubectl create serviceaccount "$ksa" -n "$HUB_K8S_NAMESPACE"
          run kubectl annotate serviceaccount -n "$HUB_K8S_NAMESPACE" "$ksa" "iam.gke.io/gcp-service-account=${gsa}" --overwrite
        else
          kubectl get serviceaccount -n "$HUB_K8S_NAMESPACE" "$ksa" >/dev/null 2>&1 || run kubectl create serviceaccount -n "$HUB_K8S_NAMESPACE" "$ksa"
          run kubectl annotate serviceaccount -n "$HUB_K8S_NAMESPACE" "$ksa" "iam.gke.io/gcp-service-account=${gsa}" --overwrite
        fi
      }
      ensure_ksa scion-worker "$WORKER_SA"
      ensure_ksa scion-factory-control "$CONTROL_SA"
      if [[ "$deploys" == 1 ]]; then
        ensure_ksa scion-deployer "$DEPLOYER_SA"
      fi

      if [[ -n "$HUB_VM_SA" ]]; then
        HUB_SA_UID="$(gcloud iam service-accounts describe "$HUB_VM_SA" --project="$HUB_PROJECT" --format='value(uniqueId)' 2>/dev/null || true)"
        RBAC_YAML="apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ${HUB_K8S_NAMESPACE}-sa-reader
  namespace: ${HUB_K8S_NAMESPACE}
rules:
- apiGroups: [\"\"]
  resources: [\"serviceaccounts\"]
  verbs: [\"get\", \"list\"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${HUB_K8S_NAMESPACE}-sa-reader
  namespace: ${HUB_K8S_NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: ${HUB_K8S_NAMESPACE}-sa-reader
subjects:
- apiGroup: rbac.authorization.k8s.io
  kind: User
  name: ${HUB_VM_SA}
- kind: ServiceAccount
  name: scion-hub
  namespace: ${HUB_K8S_NAMESPACE}
"
        if [[ -n "$HUB_SA_UID" && "$HUB_SA_UID" != "$HUB_VM_SA" ]]; then
          RBAC_YAML+="- apiGroup: rbac.authorization.k8s.io
  kind: User
  name: \"${HUB_SA_UID}\"
"
        fi
        if [[ "$DRY_RUN" == "1" ]]; then
          echo "+ kubectl apply -f - (${HUB_K8S_NAMESPACE}-sa-reader)"
        else
          printf '%s' "$RBAC_YAML" | kubectl apply -f - >/dev/null
        fi
      fi
    fi
  fi
fi

printf '\ndone%s\n' "$([[ "$DRY_RUN" == "1" ]] && echo ' (dry run: nothing was changed)')"
