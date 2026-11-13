#!/usr/bin/env bash
set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE=""
CLI_TIER=""
CLI_HUB_MODE=""
CLI_SCION_REPO=""
CLI_HARNESSES=""
CLI_IMAGE_TAG=""
CLI_WORK_DIR=""
CLI_APPROVE_APPLY=""
CLI_DRY_RUN=""
CLI_CREATE_PROJECT=""
CLI_FOLDER_ID=""
CLI_ORG_ID=""
CLI_BILLING_ACCOUNT=""
CLI_WRITE_ENV_FILE=""
CLI_PRE_HOOKS=()
CLI_POST_HOOKS=()
CLI_SKIP_SHARED_INFRA=""
CLI_GKE_PROJECT=""
CLI_GKE_CLUSTER=""
CLI_GKE_REGION=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --env <file>           Load environment variables from file
  --tier <tier>          Provisioning tier: terraform-ha (default) or hybrid-gke
  --hub-mode <mode>      Hub deployment mode: cloudrun (default) or gke
  --scion-repo <dir>     Path to Scion repository checkout (auto-cloned when omitted)
  --harnesses <list>     Comma-separated harnesses to build (default: claude,antigravity)
  --image-tag <tag>      Container image tag (default: UTC timestamp)
  --work-dir <dir>       Working directory for tfvars and build configs (default: build/gke-hub)
  --create-project       Create Hub GCP project and link billing before bootstrap
  --folder-id <id>       GCP folder ID when creating a new project
  --org-id <id>          GCP organization ID when creating a new project
  --billing-account <id> Billing account ID when creating a new project
  --write-env <file>     Atomically write resolved Hub keys into env file (mode 0600)
  --skip-shared-infra    Skip configurations/shared-infra and reuse the existing GKE cluster
  --gke-project <id>     GCP project hosting the GKE cluster (default: HUB_PROJECT)
  --gke-cluster <name>   Existing GKE cluster name override (default: <SHARED_PREFIX>-agents)
  --gke-region <region>  GKE cluster region/location override (default: HUB_REGION)
  --pre-hook <script>    Run hook script before bootstrap (repeatable)
  --post-hook <script>   Run hook script after bootstrap (repeatable)
  --approve-apply        Execute Terraform apply, Cloud Build, and IAM changes
  --dry-run              Print commands without mutating GCP or local files (or DRY_RUN=1)
  -h, --help             Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --tier) CLI_TIER="$2"; shift 2 ;;
    --hub-mode) CLI_HUB_MODE="$2"; shift 2 ;;
    --scion-repo) CLI_SCION_REPO="$2"; shift 2 ;;
    --harnesses) CLI_HARNESSES="$2"; shift 2 ;;
    --image-tag) CLI_IMAGE_TAG="$2"; shift 2 ;;
    --work-dir) CLI_WORK_DIR="$2"; shift 2 ;;
    --create-project) CLI_CREATE_PROJECT="1"; shift 1 ;;
    --folder-id) CLI_FOLDER_ID="$2"; shift 2 ;;
    --org-id) CLI_ORG_ID="$2"; shift 2 ;;
    --billing-account) CLI_BILLING_ACCOUNT="$2"; shift 2 ;;
    --write-env) CLI_WRITE_ENV_FILE="$2"; shift 2 ;;
    --pre-hook) CLI_PRE_HOOKS+=("$2"); shift 2 ;;
    --post-hook) CLI_POST_HOOKS+=("$2"); shift 2 ;;
    --skip-shared-infra) CLI_SKIP_SHARED_INFRA="1"; shift 1 ;;
    --gke-project) CLI_GKE_PROJECT="$2"; shift 2 ;;
    --gke-cluster) CLI_GKE_CLUSTER="$2"; shift 2 ;;
    --gke-region) CLI_GKE_REGION="$2"; shift 2 ;;
    --approve-apply) CLI_APPROVE_APPLY="1"; shift 1 ;;
    --dry-run) CLI_DRY_RUN="1"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

_PRESERVE_VARS=(
  HUB_PROJECT SANDBOX_PROJECT SANDBOX_REGION FIRESTORE_PROJECT DATA_BUCKET
  HUB_REGION HUB_ZONE SHARED_PREFIX HUB_NAME PO_EMAIL
  SQL_AVAILABILITY_TYPE STATE_BUCKET IMAGE_REGISTRY VERTEX_LOCATION GOOGLE_CLOUD_LOCATION
  EXTRA_ADMIN_EMAILS EXTRA_IAP_MEMBERS IAP_OAUTH_CLIENT_ID HUB_IMPERSONATE_SA USE_OPERATOR_SA
  FOLDER_ID ORG_ID BILLING_ACCOUNT SCION_GIT_URL SCION_REF DRY_RUN BUILD_POLL_SEC SKIP_EXISTING_IMAGES
  CREATE_PROJECT WRITE_ENV_FILE SCION_REPO TIER HUB_MODE HARNESSES IMAGE_TAG WORK_DIR
  PRE_HOOKS_ENV POST_HOOKS_ENV
  SKIP_SHARED_INFRA HUB_GKE_PROJECT HUB_GKE_CLUSTER HUB_GKE_REGION HUB_GKE_LOCATION
  EXTERNAL_GKE_PROJECT_ID EXTERNAL_GKE_CLUSTER_NAME RUNTIME_PROFILE
)
for _v in "${_PRESERVE_VARS[@]}"; do
  if [[ -n "${!_v:-}" ]]; then
    export "_CALLER_${_v}=${!_v}"
  fi
done

if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

for _v in "${_PRESERVE_VARS[@]}"; do
  _cv="_CALLER_${_v}"
  if [[ -n "${!_cv:-}" ]]; then
    export "${_v}=${!_cv}"
    unset "${_cv}"
  fi
done

TIER="${TIER:-terraform-ha}"
HUB_MODE="${HUB_MODE:-cloudrun}"
SCION_REPO="${SCION_REPO:-}"
HARNESSES="${HARNESSES:-claude,antigravity}"
IMAGE_TAG="${IMAGE_TAG:-v$(date -u +%Y%m%d%H%M%S)}"
WORK_DIR="${WORK_DIR:-build/gke-hub}"
APPROVE_APPLY=0
DRY_RUN="${DRY_RUN:-0}"
CREATE_PROJECT="${CREATE_PROJECT:-0}"
FOLDER_ID="${FOLDER_ID:-}"
ORG_ID="${ORG_ID:-}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
WRITE_ENV_FILE="${WRITE_ENV_FILE:-}"
SKIP_SHARED_INFRA="${SKIP_SHARED_INFRA:-0}"

[[ -n "$CLI_TIER" ]] && TIER="$CLI_TIER"
[[ -n "$CLI_HUB_MODE" ]] && HUB_MODE="$CLI_HUB_MODE"
[[ -n "$CLI_SCION_REPO" ]] && SCION_REPO="$CLI_SCION_REPO"
[[ -n "$CLI_HARNESSES" ]] && HARNESSES="$CLI_HARNESSES"
[[ -n "$CLI_IMAGE_TAG" ]] && IMAGE_TAG="$CLI_IMAGE_TAG"
[[ -n "$CLI_WORK_DIR" ]] && WORK_DIR="$CLI_WORK_DIR"
[[ -n "$CLI_APPROVE_APPLY" ]] && APPROVE_APPLY="$CLI_APPROVE_APPLY"
[[ -n "$CLI_DRY_RUN" ]] && DRY_RUN="$CLI_DRY_RUN"
[[ -n "$CLI_CREATE_PROJECT" ]] && CREATE_PROJECT="$CLI_CREATE_PROJECT"
[[ -n "$CLI_FOLDER_ID" ]] && FOLDER_ID="$CLI_FOLDER_ID"
[[ -n "$CLI_ORG_ID" ]] && ORG_ID="$CLI_ORG_ID"
[[ -n "$CLI_BILLING_ACCOUNT" ]] && BILLING_ACCOUNT="$CLI_BILLING_ACCOUNT"
[[ -n "$CLI_WRITE_ENV_FILE" ]] && WRITE_ENV_FILE="$CLI_WRITE_ENV_FILE"
[[ -n "$CLI_SKIP_SHARED_INFRA" ]] && SKIP_SHARED_INFRA="$CLI_SKIP_SHARED_INFRA"

HUB_PROJECT="${HUB_PROJECT:-}"
SHARED_PREFIX="${SHARED_PREFIX:-tfha}"
HUB_NAME="${HUB_NAME:-prod}"
PO_EMAIL="${PO_EMAIL:-}"

if [[ -z "$HUB_PROJECT" || -z "$PO_EMAIL" ]]; then
  echo "HUB_PROJECT and PO_EMAIL must be set." >&2
  exit 1
fi

if [[ "$CREATE_PROJECT" == "1" ]]; then
  _DEFAULT_HUB_REGION="us-east1"
  SQL_AVAILABILITY_TYPE="${SQL_AVAILABILITY_TYPE:-ZONAL}"
else
  _DEFAULT_HUB_REGION="us-central1"
  SQL_AVAILABILITY_TYPE="${SQL_AVAILABILITY_TYPE:-REGIONAL}"
fi
HUB_REGION="${HUB_REGION:-${SANDBOX_REGION:-$_DEFAULT_HUB_REGION}}"
HUB_ZONE="${HUB_ZONE:-${HUB_REGION}-b}"
USE_OPERATOR_SA="${USE_OPERATOR_SA:-$([[ "$CREATE_PROJECT" == "1" && "$HUB_MODE" == "cloudrun" ]] && echo 1 || echo 0)}"
STATE_BUCKET="${STATE_BUCKET:-${HUB_PROJECT}-${SHARED_PREFIX}-tfstate}"
BUILD_POLL_SEC="${BUILD_POLL_SEC:-10}"
# Platforms for the Scion base and harness images. Cloud Run and GKE Autopilot run amd64 only, and the
# upstream arm64 half builds under emulation (about 60 of the 90 cold-start minutes in hillclimb Run 1).
# Set IMAGE_PLATFORMS=all to keep the upstream linux/amd64,linux/arm64 builds.
IMAGE_PLATFORMS="${IMAGE_PLATFORMS:-linux/amd64}"
SKIP_EXISTING_IMAGES="${SKIP_EXISTING_IMAGES:-0}"

if [[ "$TIER" == "terraform-ha" ]]; then
  if [[ "$HUB_NAME" != "${SHARED_PREFIX}-"* ]]; then
    HUB_NAME="${SHARED_PREFIX}-${HUB_NAME}"
  fi
  if [[ "$HUB_MODE" == "cloudrun" ]]; then
    HUB_TF_DIR="hub"
  else
    HUB_TF_DIR="hub-gke"
  fi
  DEFAULT_CLUSTER_NAME="${SHARED_PREFIX}-agents"
  HUB_K8S_NAMESPACE="${HUB_NAME}"
  IMAGE_REGISTRY="${IMAGE_REGISTRY:-${HUB_REGION}-docker.pkg.dev/${HUB_PROJECT}/${SHARED_PREFIX}-scion}"
else
  DEFAULT_CLUSTER_NAME="scion-hub-${HUB_NAME}"
  HUB_K8S_NAMESPACE="scion-hub-${HUB_NAME}"
  IMAGE_REGISTRY="${IMAGE_REGISTRY:-${HUB_REGION}-docker.pkg.dev/${HUB_PROJECT}/scion}"
fi

HUB_GKE_PROJECT="${CLI_GKE_PROJECT:-${HUB_GKE_PROJECT:-${EXTERNAL_GKE_PROJECT_ID:-$HUB_PROJECT}}}"
HUB_GKE_REGION="${CLI_GKE_REGION:-${HUB_GKE_REGION:-${HUB_GKE_LOCATION:-$HUB_REGION}}}"
CLUSTER_NAME="${CLI_GKE_CLUSTER:-${HUB_GKE_CLUSTER:-${EXTERNAL_GKE_CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}}}"
USE_EXTERNAL_GKE=0
if [[ "$HUB_GKE_PROJECT" != "$HUB_PROJECT" || "$CLUSTER_NAME" != "$DEFAULT_CLUSTER_NAME" ]]; then
  USE_EXTERNAL_GKE=1
fi

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %s\n' "$*" >&2
  else
    "$@"
  fi
}

run_hooks() {
  local phase="$1"
  shift
  local hook
  export HUB_PROJECT HUB_REGION HUB_ZONE SHARED_PREFIX HUB_NAME TIER HUB_MODE
  export SCION_REPO WORK_DIR ABS_WORK_DIR CLUSTER_NAME HUB_K8S_NAMESPACE
  export IMAGE_REGISTRY IMAGE_TAG APPROVE_APPLY DRY_RUN PO_EMAIL
  export HUB_URL_OUT="${HUB_URL_OUT:-}" IAP_CLIENT_ID="${IAP_CLIENT_ID:-}"
  for hook in "$@"; do
    [[ -n "$hook" ]] || continue
    echo "== Running ${phase}-hook: ${hook}"
    if [[ -x "$hook" ]]; then
      run "$hook"
    else
      run bash "$hook"
    fi
  done
}

wait_for_cloud_build() {
  local build_id="$1"
  if [[ -z "$build_id" ]]; then
    echo "Failed to capture Cloud Build ID from submission output." >&2
    return 1
  fi
  echo "Waiting for Cloud Build ${build_id} in ${HUB_PROJECT}..."
  local poll_errs=0
  while true; do
    local status
    if status="$(gcloud builds describe "$build_id" --project="$HUB_PROJECT" --format='value(status)')" && [[ -n "$status" ]]; then
      poll_errs=0
    else
      poll_errs=$((poll_errs + 1))
      if [[ $poll_errs -lt 6 ]]; then
        echo "Transient gcloud builds describe error (${poll_errs}/6); retrying in ${BUILD_POLL_SEC}s..." >&2
        sleep "$BUILD_POLL_SEC"
        continue
      fi
      echo "Failed to poll Cloud Build ${build_id} after ${poll_errs} consecutive errors." >&2
      return 1
    fi
    case "$status" in
      SUCCESS)
        echo "Cloud Build ${build_id}: SUCCESS"
        return 0
        ;;
      QUEUED|PENDING|WORKING)
        sleep "$BUILD_POLL_SEC"
        ;;
      *)
        echo "Cloud Build ${build_id} ended with status: ${status}" >&2
        gcloud builds log "$build_id" --project="$HUB_PROJECT" 2>/dev/null | tail -n 80 >&2 || true
        return 1
        ;;
    esac
  done
}

submit_and_wait() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %s\n' "$*" >&2
    return 0
  fi
  local out build_id attempt
  for attempt in 1 2 3 4; do
    set +e
    out="$(GCLOUD_PROJECT="$HUB_PROJECT" "$@" 2>&1 | tee /dev/stderr)"
    local rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      break
    fi
    if [[ "$out" == *"storage.objects.get"* && $attempt -lt 4 ]]; then
      echo "Waiting for Cloud Build SA IAM propagation (attempt ${attempt}/4)..." >&2
      sleep 10
      continue
    fi
    return "$rc"
  done
  build_id="$(printf '%s\n' "$out" | grep -Eo 'builds/[0-9a-fA-F-]{36}' | tail -n 1 | cut -d/ -f2 || true)"
  if [[ -z "$build_id" ]]; then
    build_id="$(printf '%s\n' "$out" | grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' | tail -n 1 || true)"
  fi
  wait_for_cloud_build "$build_id"
}

image_exists_in_ar() {
  local img="$1"
  [[ "$SKIP_EXISTING_IMAGES" == "1" ]] || return 1
  local desc
  desc="$(gcloud artifacts docker images describe "$img" --project="$HUB_PROJECT" --format='value(image_summary.digest)' 2>/dev/null || true)"
  [[ "$desc" == sha256:* ]]
}

resolve_harness_step() {
  local h="$1"
  h="${h#scion-}"
  if [[ "$h" == "gemini" && ! -d "${SCION_REPO}/harnesses/gemini" && ( ! -d "${SCION_REPO}" || -d "${SCION_REPO}/harnesses/gemini-cli" ) ]]; then
    echo "scion-gemini-cli"
  else
    echo "scion-${h}"
  fi
}

discover_cloudrun_url() {
  local raw
  raw="$(gcloud run services describe "$HUB_NAME" --region="$HUB_REGION" --project="$HUB_PROJECT" \
    --format='value(status.url,metadata.annotations."run.googleapis.com/urls")' 2>/dev/null || true)"
  printf '%s\n' "$raw" | grep -Eo 'https://[A-Za-z0-9._-]+' | head -n 1 || true
}

discover_iap_client_id() {
  local cid=""
  local brands brand clients
  brands="$(gcloud alpha iap oauth-brands list --project="$HUB_PROJECT" --format='value(name)' 2>/dev/null || true)"
  for brand in $brands; do
    clients="$(gcloud alpha iap oauth-clients list "$brand" --format='value(name)' 2>/dev/null || true)"
    for c in $clients; do
      cid="${c##*/}"
      if [[ "$cid" == *.apps.googleusercontent.com ]]; then
        echo "$cid"
        return 0
      fi
    done
  done
  local url="$1"
  if [[ -n "$url" ]]; then
    cid="$(curl -sI --max-time 10 "$url" 2>/dev/null | grep -Eo '[0-9]+-[0-9a-zA-Z_]+\.apps\.googleusercontent\.com' | head -n 1 || true)"
    if [[ -n "$cid" ]]; then
      echo "$cid"
      return 0
    fi
  fi
  return 0
}

write_hub_tfvars() {
  local out_file="$1"
  local iap_cid="${2:-}"

  local -a admins=("$PO_EMAIL")
  if [[ -n "${EXTRA_ADMIN_EMAILS:-}" ]]; then
    IFS=',' read -ra _extra_a <<< "$EXTRA_ADMIN_EMAILS"
    for e in "${_extra_a[@]}"; do
      [[ -n "$e" ]] && admins+=("$e")
    done
  fi
  local admins_hcl=""
  for e in "${admins[@]}"; do
    admins_hcl+="${admins_hcl:+, }\"${e}\""
  done

  local -a iap_m=("user:${PO_EMAIL}")
  if [[ -n "${EXTRA_IAP_MEMBERS:-}" ]]; then
    IFS=',' read -ra _extra_m <<< "$EXTRA_IAP_MEMBERS"
    for m in "${_extra_m[@]}"; do
      [[ -n "$m" ]] && iap_m+=("$m")
    done
  fi
  local iap_hcl=""
  for m in "${iap_m[@]}"; do
    iap_hcl+="${iap_hcl:+, }\"${m}\""
  done

  cat > "$out_file" <<EOF
project_id    = "${HUB_PROJECT}"
region        = "${HUB_REGION}"
zone          = "${HUB_ZONE}"
shared_prefix = "${SHARED_PREFIX}"
hub_name      = "${HUB_NAME}"
state_prefix  = "${SHARED_PREFIX}/hubs/${HUB_NAME}"
admin_emails  = [${admins_hcl}]
iap_members   = [${iap_hcl}]
EOF

  if [[ "$HUB_MODE" == "cloudrun" ]]; then
    cat >> "$out_file" <<EOF
hub_image     = "${IMAGE_REGISTRY}/hub:${IMAGE_TAG}"
max_instances = ${HUB_MAX_INSTANCES:-1}
EOF
  else
    cat >> "$out_file" <<EOF
hostname         = "${HUB_HOSTNAME:-${HUB_NAME}.example.com}"
hub_image_digest = "${HUB_IMAGE_DIGEST:-sha256:0000000000000000000000000000000000000000000000000000000000000000}"
EOF
  fi

  if [[ -n "$iap_cid" ]]; then
    echo "iap_oauth_client_id = \"${iap_cid}\"" >> "$out_file"
  fi
  if [[ "$USE_EXTERNAL_GKE" == "1" && "${HAS_HUB_EXTERNAL_GKE_TF:-0}" == "1" ]]; then
    cat >> "$out_file" <<EOF
external_gke_project_id   = "${HUB_GKE_PROJECT}"
external_gke_cluster_name = "${CLUSTER_NAME}"
EOF
  fi
}

PRE_HOOKS=()
POST_HOOKS=("${CLI_POST_HOOKS[@]+"${CLI_POST_HOOKS[@]}"}")
if [[ -n "${PRE_HOOKS_ENV:-}" ]]; then
  IFS=',' read -ra _ENV_PRE <<< "$PRE_HOOKS_ENV"
  PRE_HOOKS+=("${_ENV_PRE[@]+"${_ENV_PRE[@]}"}")
fi
PRE_HOOKS+=("${CLI_PRE_HOOKS[@]+"${CLI_PRE_HOOKS[@]}"}")
if [[ -n "${POST_HOOKS_ENV:-}" ]]; then
  IFS=',' read -ra _ENV_POST <<< "$POST_HOOKS_ENV"
  POST_HOOKS+=("${_ENV_POST[@]+"${_ENV_POST[@]}"}")
fi
for _h in "${PRE_HOOKS[@]+"${PRE_HOOKS[@]}"}" "${POST_HOOKS[@]+"${POST_HOOKS[@]}"}"; do
  [[ -n "$_h" ]] || continue
  if [[ ! -f "$_h" ]]; then
    echo "Hook script not found: ${_h}" >&2
    exit 1
  fi
done

mkdir -p "$WORK_DIR"
ABS_WORK_DIR="$(cd "$WORK_DIR" && pwd)"

SCION_GIT_URL="${SCION_GIT_URL:-https://github.com/GoogleCloudPlatform/scion.git}"
# Scion revision the Hub and agent images are built from. Pinned, because this script patches Scion
# source by text and four of the eight failures in hillclimb Run 1 came from upstream changes that
# landed between runs. The pin is the commit the first end-to-end run passed on. To move it, run a
# full cold start with SCION_REF=main (or a newer commit), fix what breaks, then update this default.
SCION_PINNED_REF="6eb4b41c452ca24c4b7f798d9b36c5a7bdb93b63"
SCION_REF="${SCION_REF:-$SCION_PINNED_REF}"

if [[ -z "$SCION_REPO" ]]; then
  SCION_REPO="${ABS_WORK_DIR}/scion-src"
  _scion_ref_is_sha=0
  [[ "$SCION_REF" =~ ^[0-9a-f]{40}$ ]] && _scion_ref_is_sha=1
  if [[ "$_scion_ref_is_sha" == "1" && -d "${SCION_REPO}/.git" ]]; then
    # A cached clone of another revision (for example an earlier unpinned run) is replaced; it is
    # this script's own build cache under WORK_DIR, never a caller's checkout.
    _scion_have="$(git -C "$SCION_REPO" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$_scion_have" != "$SCION_REF" ]]; then
      echo "Scion cache is at ${_scion_have:-unknown}, want ${SCION_REF}: recloning ${SCION_REPO}." >&2
      run rm -rf "$SCION_REPO"
    fi
  fi
  if [[ ! -d "${SCION_REPO}/.git" && ! -d "${SCION_REPO}/deploy/terraform" ]]; then
    if [[ "$_scion_ref_is_sha" == "1" ]]; then
      # `git clone -b` takes a branch or tag, not a commit: fetch the commit itself.
      run git init -q "$SCION_REPO"
      run git -C "$SCION_REPO" remote add origin "$SCION_GIT_URL"
      run git -C "$SCION_REPO" fetch -q --depth 1 origin "$SCION_REF"
      run git -C "$SCION_REPO" checkout -q FETCH_HEAD
    else
      run git clone --depth 1 -b "$SCION_REF" "$SCION_GIT_URL" "$SCION_REPO"
    fi
  fi
fi

HAS_SHARED_EXTERNAL_GKE_TF=0
HAS_HUB_EXTERNAL_GKE_TF=0
if [[ "$TIER" == "terraform-ha" ]]; then
  SHARED_TF_PATH="${SCION_REPO}/deploy/terraform/configurations/shared-infra"
  HUB_TF_PATH="${SCION_REPO}/deploy/terraform/configurations/${HUB_TF_DIR}"
  if grep -q 'variable "external_gke_project_id"' "${SHARED_TF_PATH}/variables.tf" 2>/dev/null; then
    HAS_SHARED_EXTERNAL_GKE_TF=1
  fi
  if grep -q 'variable "external_gke_project_id"' "${HUB_TF_PATH}/variables.tf" 2>/dev/null; then
    HAS_HUB_EXTERNAL_GKE_TF=1
  fi
  if [[ "$USE_EXTERNAL_GKE" == "1" && "$HAS_HUB_EXTERNAL_GKE_TF" != "1" ]]; then
    echo "Error: reusing a GKE cluster in another project (${HUB_GKE_PROJECT}) or with a non-default name (${CLUSTER_NAME} != ${DEFAULT_CLUSTER_NAME}) requires '--scion-repo' to declare 'external_gke_project_id' and 'external_gke_cluster_name' in deploy/terraform/configurations/${HUB_TF_DIR}/variables.tf. For same-project reuse of '${DEFAULT_CLUSTER_NAME}', use '--skip-shared-infra'." >&2
    exit 1
  fi
fi

if [[ ${#PRE_HOOKS[@]} -gt 0 ]]; then
  run_hooks "pre" "${PRE_HOOKS[@]}"
fi

OPERATOR_SA_NAME="${OPERATOR_SA_NAME:-factory-operator}"
OPERATOR_SA_EMAIL="${OPERATOR_SA_NAME}@${HUB_PROJECT}.iam.gserviceaccount.com"

if [[ "$CREATE_PROJECT" == "1" ]]; then
  echo "== 0. Cold-Start GCP Project Bootstrap (${HUB_PROJECT})"
  if ! gcloud projects describe "$HUB_PROJECT" >/dev/null 2>&1; then
    RESOLVED_FOLDER="${FOLDER_ID#folders/}"
    RESOLVED_ORG="${ORG_ID#organizations/}"
    if [[ -z "$RESOLVED_FOLDER" && -z "$RESOLVED_ORG" ]]; then
      ACTIVE_PROJ="$(gcloud config get-value project 2>/dev/null | tr -d '[:space:]' || true)"
      if [[ -n "$ACTIVE_PROJ" && "$ACTIVE_PROJ" != "(unset)" && "$ACTIVE_PROJ" != "$HUB_PROJECT" ]]; then
        PARENT_INFO="$(gcloud projects describe "$ACTIVE_PROJ" --format='value(parent.type,parent.id)' 2>/dev/null || true)"
        if [[ -n "$PARENT_INFO" ]]; then
          read -r _ptype _pid <<< "$PARENT_INFO"
          if [[ "$_ptype" == "folder" || "$_ptype" == "folders" ]]; then
            RESOLVED_FOLDER="${_pid#folders/}"
          elif [[ "$_ptype" == "organization" || "$_ptype" == "organizations" ]]; then
            RESOLVED_ORG="${_pid#organizations/}"
          fi
        fi
      fi
    fi
    if [[ -z "$RESOLVED_FOLDER" && -z "$RESOLVED_ORG" ]]; then
      ORGS_RAW="$(gcloud organizations list --format='value(name)' 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
      ORG_COUNT="$(printf '%s\n' "$ORGS_RAW" | sed '/^[[:space:]]*$/d' | wc -l | tr -d '[:space:]')"
      if [[ "$ORG_COUNT" == "1" ]]; then
        SINGLE_ORG="${ORGS_RAW##*/}"
        FOLDERS_RAW="$(gcloud resource-manager folders list --organization="$SINGLE_ORG" --format='value(name)' 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
        FOLDER_COUNT="$(printf '%s\n' "$FOLDERS_RAW" | sed '/^[[:space:]]*$/d' | wc -l | tr -d '[:space:]')"
        if [[ "$FOLDER_COUNT" == "1" ]]; then
          RESOLVED_FOLDER="${FOLDERS_RAW##*/}"
        elif [[ "$FOLDER_COUNT" == "0" ]]; then
          RESOLVED_ORG="$SINGLE_ORG"
        fi
      fi
    fi
    CREATE_PROJ_ARGS=("$HUB_PROJECT")
    if [[ -n "$RESOLVED_FOLDER" ]]; then
      CREATE_PROJ_ARGS+=("--folder=${RESOLVED_FOLDER}")
    elif [[ -n "$RESOLVED_ORG" ]]; then
      CREATE_PROJ_ARGS+=("--organization=${RESOLVED_ORG}")
    fi
    run gcloud projects create "${CREATE_PROJ_ARGS[@]}"
  fi

  RESOLVED_BILLING="${BILLING_ACCOUNT#billingAccounts/}"
  if [[ -z "$RESOLVED_BILLING" ]]; then
    BA_RAW="$(gcloud billing accounts list --filter=open=true --format='value(name)' 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
    BA_COUNT="$(printf '%s\n' "$BA_RAW" | sed '/^[[:space:]]*$/d' | wc -l | tr -d '[:space:]')"
    if [[ "$BA_COUNT" == "1" ]]; then
      RESOLVED_BILLING="${BA_RAW##*/}"
    fi
  fi
  if [[ -n "$RESOLVED_BILLING" ]]; then
    run gcloud billing projects link "$HUB_PROJECT" --billing-account="${RESOLVED_BILLING}"
  elif [[ "$DRY_RUN" == "0" ]]; then
    echo "Could not resolve a single open billing account; pass --billing-account <id>." >&2
    exit 1
  fi
fi

echo "== 1. Enable APIs & Terraform State Bucket"
run gcloud services enable --project="$HUB_PROJECT" \
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
  compute.googleapis.com servicenetworking.googleapis.com container.googleapis.com run.googleapis.com \
  file.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com iap.googleapis.com aiplatform.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com storage.googleapis.com firestore.googleapis.com \
  logging.googleapis.com cloudtrace.googleapis.com monitoring.googleapis.com

if [[ "$CREATE_PROJECT" == "1" ]]; then
  # A just-enabled Compute Engine API can still answer SERVICE_DISABLED for a short time on a brand-new
  # project (hillclimb Run 1, third attempt), so retry instead of failing the cold start.
  _net_ready=0
  for _net_try in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if gcloud compute networks describe default --project="$HUB_PROJECT" >/dev/null 2>&1 \
      || run gcloud compute networks create default --project="$HUB_PROJECT" --subnet-mode=auto; then
      _net_ready=1
      break
    fi
    echo "Waiting for the Compute Engine API to propagate (attempt ${_net_try}/12)..." >&2
    [[ $_net_try -lt 12 ]] && sleep "${BUILD_POLL_SEC}"
  done
  if [[ "$_net_ready" == "0" ]]; then
    echo "Failed to create the default VPC network in ${HUB_PROJECT} after 12 attempts." >&2
    exit 1
  fi
  if ! gcloud iam service-accounts describe "$OPERATOR_SA_EMAIL" --project="$HUB_PROJECT" >/dev/null 2>&1; then
    run gcloud iam service-accounts create "$OPERATOR_SA_NAME" \
      --project="$HUB_PROJECT" \
      --display-name="Scion Factory Operator"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    run gcloud iam service-accounts add-iam-policy-binding "$OPERATOR_SA_EMAIL" \
      --project="$HUB_PROJECT" \
      --member="user:${PO_EMAIL}" \
      --role="roles/iam.serviceAccountTokenCreator"
  else
    _sa_bound=0
    for _sa_try in 1 2 3 4 5; do
      if gcloud iam service-accounts add-iam-policy-binding "$OPERATOR_SA_EMAIL" \
        --project="$HUB_PROJECT" \
        --member="user:${PO_EMAIL}" \
        --role="roles/iam.serviceAccountTokenCreator" >/dev/null 2>&1; then
        _sa_bound=1
        break
      fi
      [[ $_sa_try -lt 5 ]] && sleep "${BUILD_POLL_SEC}"
    done
    if [[ "$_sa_bound" == "0" ]]; then
      echo "Failed to bind roles/iam.serviceAccountTokenCreator on ${OPERATOR_SA_EMAIL} for user:${PO_EMAIL} after 5 attempts." >&2
      exit 1
    fi
  fi
  if [[ ",${EXTRA_ADMIN_EMAILS:-}," != *",${OPERATOR_SA_EMAIL},"* ]]; then
    EXTRA_ADMIN_EMAILS="${EXTRA_ADMIN_EMAILS:+${EXTRA_ADMIN_EMAILS},}${OPERATOR_SA_EMAIL}"
  fi
  if [[ ",${EXTRA_IAP_MEMBERS:-}," != *",serviceAccount:${OPERATOR_SA_EMAIL},"* ]]; then
    EXTRA_IAP_MEMBERS="${EXTRA_IAP_MEMBERS:+${EXTRA_IAP_MEMBERS},}serviceAccount:${OPERATOR_SA_EMAIL}"
  fi
  if [[ "${USE_OPERATOR_SA:-0}" == "1" ]]; then
    HUB_IMPERSONATE_SA="${HUB_IMPERSONATE_SA:-$OPERATOR_SA_EMAIL}"
  fi
fi

if [[ "$TIER" == "terraform-ha" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    run gcloud storage buckets create "gs://${STATE_BUCKET}" --project="$HUB_PROJECT" --location="$HUB_REGION" --uniform-bucket-level-access
    run gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
  else
    if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$HUB_PROJECT" >/dev/null 2>&1; then
      run gcloud storage buckets create "gs://${STATE_BUCKET}" --project="$HUB_PROJECT" --location="$HUB_REGION" --uniform-bucket-level-access
    fi
    run gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
  fi

  if [[ "$SKIP_SHARED_INFRA" == "1" ]]; then
    echo "== 2. Terraform HA: shared-infra (${SHARED_PREFIX}) [skipped - reusing ${HUB_GKE_PROJECT}/${CLUSTER_NAME}]"
    if [[ "$DRY_RUN" == "1" ]]; then
      run gcloud container clusters describe "$CLUSTER_NAME" --project="$HUB_GKE_PROJECT" --region="$HUB_GKE_REGION"
    else
      gcloud container clusters describe "$CLUSTER_NAME" --project="$HUB_GKE_PROJECT" --region="$HUB_GKE_REGION" --format='value(name)' >/dev/null || {
        echo "Error: --skip-shared-infra requires existing GKE cluster '${CLUSTER_NAME}' in project '${HUB_GKE_PROJECT}' (${HUB_GKE_REGION}). Run once without --skip-shared-infra first." >&2
        exit 1
      }
    fi
  else
    echo "== 2. Terraform HA: shared-infra (${SHARED_PREFIX})"
    cat > "${ABS_WORK_DIR}/shared-infra.tfvars" <<EOF
project_id            = "${HUB_PROJECT}"
region                = "${HUB_REGION}"
zone                  = "${HUB_ZONE}"
name_prefix           = "${SHARED_PREFIX}"
sql_availability_type = "${SQL_AVAILABILITY_TYPE}"
EOF
    if [[ "$USE_EXTERNAL_GKE" == "1" && "$HAS_SHARED_EXTERNAL_GKE_TF" == "1" ]]; then
      cat >> "${ABS_WORK_DIR}/shared-infra.tfvars" <<EOF
external_gke_project_id   = "${HUB_GKE_PROJECT}"
external_gke_cluster_name = "${CLUSTER_NAME}"
EOF
    fi

    SHARED_TF_PATH="${SCION_REPO}/deploy/terraform/configurations/shared-infra"
    run terraform -chdir="${SHARED_TF_PATH}" init -reconfigure \
      -backend-config="bucket=${STATE_BUCKET}" \
      -backend-config="prefix=${SHARED_PREFIX}/shared-infra"
    run terraform -chdir="${SHARED_TF_PATH}" plan -var-file="${ABS_WORK_DIR}/shared-infra.tfvars"
    if [[ "$APPROVE_APPLY" == 1 && "$DRY_RUN" == 0 ]]; then
      run terraform -chdir="${SHARED_TF_PATH}" apply -auto-approve -var-file="${ABS_WORK_DIR}/shared-infra.tfvars"
    fi
  fi

  # Grant Cloud Build default SA builder + push access (modern GCP orgs disable automatic grants on default compute SA)
  REPO_NAME="$(basename "$IMAGE_REGISTRY")"
  REPO_LOC="${HUB_REGION}"
  if [[ "$IMAGE_REGISTRY" == *-docker.pkg.dev/* ]]; then
    REPO_LOC="${IMAGE_REGISTRY%%-docker.pkg.dev/*}"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    run gcloud projects add-iam-policy-binding "$HUB_PROJECT" \
      --member="serviceAccount:cloud-build-default" --role="roles/cloudbuild.builds.builder"
    run gcloud artifacts repositories add-iam-policy-binding "$REPO_NAME" \
      --project="$HUB_PROJECT" --location="$REPO_LOC" \
      --member="serviceAccount:cloud-build-default" --role="roles/artifactregistry.writer"
  elif [[ "$APPROVE_APPLY" == 1 ]]; then
    CB_SA_PATH="$(gcloud builds get-default-service-account --project="$HUB_PROJECT" --format='value(serviceAccountEmail)' 2>/dev/null || true)"
    CB_SA="${CB_SA_PATH##*/}"
    if [[ -n "$CB_SA" ]]; then
      for _cb_try in 1 2 3 4 5 6; do
        if gcloud projects add-iam-policy-binding "$HUB_PROJECT" \
          --member="serviceAccount:${CB_SA}" --role="roles/cloudbuild.builds.builder" --condition=None >/dev/null 2>&1 \
          || gcloud projects add-iam-policy-binding "$HUB_PROJECT" \
          --member="serviceAccount:${CB_SA}" --role="roles/cloudbuild.builds.builder" >/dev/null 2>&1; then
          break
        fi
        [[ $_cb_try -lt 6 ]] && sleep "${BUILD_POLL_SEC}"
      done
      for _ar_try in 1 2 3 4 5 6; do
        if gcloud artifacts repositories add-iam-policy-binding "$REPO_NAME" \
          --project="$HUB_PROJECT" --location="$REPO_LOC" \
          --member="serviceAccount:${CB_SA}" --role="roles/artifactregistry.writer" >/dev/null 2>&1; then
          break
        fi
        [[ $_ar_try -lt 6 ]] && sleep "${BUILD_POLL_SEC}"
      done
    fi
  fi
else
  REPO_NAME="$(basename "$IMAGE_REGISTRY")"
  REPO_LOC="${HUB_REGION}"
  if [[ "$DRY_RUN" == "0" ]]; then
    if ! gcloud artifacts repositories describe "$REPO_NAME" --project="$HUB_PROJECT" --location="$REPO_LOC" >/dev/null 2>&1; then
      run gcloud artifacts repositories create "$REPO_NAME" --project="$HUB_PROJECT" --location="$REPO_LOC" --repository-format=docker || true
    fi
  else
    run gcloud artifacts repositories create "$REPO_NAME" --project="$HUB_PROJECT" --location="$REPO_LOC" --repository-format=docker
  fi
fi

echo "== 3. Build Hub & Multi-Arch Harness Images"
if [[ "$TIER" == "terraform-ha" && "$HUB_MODE" == "cloudrun" ]]; then
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/pkg/runtimebroker/nfs_mount.go" ]]; then
    python3 - "${SCION_REPO}/pkg/runtimebroker/nfs_mount.go" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
if "/var/lib/volumes/nfs/" not in src:
    old = "if mounted && currentServerExport == wantServerExport {"
    new = 'if mounted && (currentServerExport == wantServerExport || (!mounts && strings.HasPrefix(currentServerExport, "/var/lib/volumes/nfs/"))) {'
    if old in src:
        p.write_text(src.replace(old, new, 1))
PY
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/pkg/agent/run.go" ]]; then
    python3 - "${SCION_REPO}/pkg/agent/run.go" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
marker = "config.IsSharedProjectConfigDir(projectDir) && !explicitWorkspace && !opts.SharedWorkspace"
if marker not in src:
    pat = re.compile(
        r'(?m)^([ \t]*)(if legacy := legacyAgentWorkspaceRoot\(projectDir, opts\.Name, agentDir, explicitWorkspace \|\| opts\.SharedWorkspace\); legacy != "" \{\n'
        r'[ \t]*roots = append\(roots, legacy\)\n'
        r'[ \t]*\})'
    )
    def _repl(m):
        ind = m.group(1)
        return (
            f"{m.group(0)}\n"
            f'{ind}if config.IsSharedProjectConfigDir(projectDir) && !explicitWorkspace && !opts.SharedWorkspace && agentDir != "" {{\n'
            f'{ind}\tif extDir, extErr := config.AgentDirForProject(projectDir, opts.Name, false, hubDispatchedProjectID); extErr == nil && extDir != "" && filepath.Clean(agentDir) == filepath.Clean(extDir) {{\n'
            f'{ind}\t\troots = append(roots, filepath.Join(agentDir, "workspace"))\n'
            f"{ind}\t}}\n"
            f"{ind}}}"
        )
    updated, count = pat.subn(_repl, src, count=1)
    if count == 1:
        p.write_text(updated)
PY
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/pkg/hub/handlers_projects_core.go" ]]; then
    python3 - "${SCION_REPO}/pkg/hub/handlers_projects_core.go" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
old = '} else if project.GitRemote == "" {'
new = '} else if project.GitRemote == "" || s.workspaceBackendShared() {'
if new not in src and old in src:
    p.write_text(src.replace(old, new, 1))
PY
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/pkg/runtime/k8s_runtime.go" ]]; then
    # Fails the bootstrap when upstream renamed the code, instead of silently skipping the patch.
    python3 "${SCRIPT_DIR}/patch_scion_shared_dir_volume.py" "${SCION_REPO}/pkg/runtime/k8s_runtime.go"
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/pkg/hub/handlers_agents_core.go" ]]; then
    python3 - "${SCION_REPO}/pkg/hub/handlers_agents_core.go" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
old = 'if s.transportMinter != nil && s.transportAudience != "" {'
new = 'if s.transportMinter != nil && s.transportAudience != "" && !strings.HasPrefix(s.transportAudience, "369001918367-") {'
if new not in src and old in src:
    updated = src.replace(old, new, 1)
    if not re.search(r'(?m)^\s*"strings"\s*$', updated) and 'import "strings"' not in updated:
        if re.search(r'(?m)^import\s*\(\n', updated):
            updated = re.sub(r'(?m)^(import\s*\(\n)', r'\1\t"strings"\n', updated, count=1)
        elif re.search(r'(?m)^package\s+\w+\s*$', updated):
            updated = re.sub(r'(?m)^(package\s+\w+\s*)$', r'\1\n\nimport "strings"', updated, count=1)
    p.write_text(updated)
PY
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/deploy/terraform/modules/hub-cloudrun/templates/settings.yaml.tftpl" ]]; then
    # 1. Current Scion ignores template telemetry blocks on Hub dispatch (ptone/scion#4524); only the Hub
    #    settings `telemetry` section reaches runtime brokers and injects SCION_GCP_PROJECT_ID into pods.
    # 2. On Pass 1 of a fresh GCP project (`iap_oauth_client_id == null`), upstream omits `oidc_audience`,
    #    which causes `scion server` to exit(1) with `hosted HA deployment requires server.auth.transport.oidc_audience`
    #    and fails Cloud Run revision 00001 startup probes (Error code 9). Defaulting Pass 1 to `public_url`
    #    lets revision 00001 pass startup cleanly before Pass 2 binds the discovered Google-managed IAP client ID.
    python3 - "${SCION_REPO}/deploy/terraform/modules/hub-cloudrun/templates/settings.yaml.tftpl" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
old_oidc = (
    "%{ if iap_oauth_client_id != null ~}\n"
    "      oidc_audience: ${iap_oauth_client_id}\n"
    "%{ endif ~}"
)
new_oidc = "      oidc_audience: ${coalesce(iap_oauth_client_id, public_url)}"
if old_oidc in src:
    src = src.replace(old_oidc, new_oidc, 1)
if not re.search(r"(?m)^telemetry:\s*$", src):
    block = (
        "\ntelemetry:\n"
        "  enabled: true\n"
        "  cloud:\n"
        "    enabled: true\n"
        "    provider: gcp\n"
        "    gcp_project_id: ${project_id}\n"
    )
    src = src.rstrip("\n") + "\n" + block
p.write_text(src)
PY
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/deploy/terraform/modules/hub-cloudrun/main.tf" ]]; then
    # Conditioned Secret Manager IAM on a brand-new GCP project takes ~150-180s to propagate; upstream's
    # 120s timer lets Pass 1 Cloud Run revision 00001 boot too early and fail its startup probe (Error code 9).
    python3 - "${SCION_REPO}/deploy/terraform/modules/hub-cloudrun/main.tf" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
old = 'create_duration = "120s"'
new = 'create_duration = "180s"'
if new not in src and old in src:
    p.write_text(src.replace(old, new, 1))
PY
  fi
  if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && -f "${SCION_REPO}/image-build/scripts/build-images.sh" ]]; then
    # Suppress build-images.sh's standalone-harness warning when setup-gke-hub.sh just built scion-base at IMAGE_TAG.
    python3 - "${SCION_REPO}/image-build/scripts/build-images.sh" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
old = 'if [[ "${needs_scion_base}" != "true" || "${built_scion_base}" == "true" ]]; then'
new = 'if [[ "${needs_scion_base}" != "true" || "${built_scion_base}" == "true" || "${SCION_BASE_JUST_BUILT:-0}" == "1" ]]; then'
if new not in src and old in src:
    p.write_text(src.replace(old, new, 1))
PY
  fi
  CLOUDRUN_IGNORE="${ABS_WORK_DIR}/gcloudignore-cloudrun"
  if [[ -f "${SCION_REPO}/image-build/gcloudignore-omni" ]]; then
    grep -Ev '^(/scripts/|#!include:)' "${SCION_REPO}/image-build/gcloudignore-omni" > "$CLOUDRUN_IGNORE" || true
  else
    printf '.git/\nweb/node_modules/\n*_test.go\n' > "$CLOUDRUN_IGNORE"
  fi
  printf '\n/deploy/\n.terraform/\n**/.terraform/\n/bin/\n' >> "$CLOUDRUN_IGNORE"
  CLOUDRUN_CB_CONFIG="${ABS_WORK_DIR}/cloudbuild-cloudrun-hub.yaml"
  cat > "$CLOUDRUN_CB_CONFIG" <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    id: 'build-cloudrun-hub'
    args: ['build', '--platform', 'linux/amd64', '-t', '${IMAGE_REGISTRY}/hub:${IMAGE_TAG}', '-f', 'scripts/cloudrun/Dockerfile', '.']
images:
  - '${IMAGE_REGISTRY}/hub:${IMAGE_TAG}'
options:
  machineType: 'E2_HIGHCPU_8'
timeout: 1800s
EOF
  if [[ "$DRY_RUN" == "1" || "$APPROVE_APPLY" == 1 ]]; then
    if ! image_exists_in_ar "${IMAGE_REGISTRY}/hub:${IMAGE_TAG}"; then
      submit_and_wait gcloud builds submit --async \
        --project="$HUB_PROJECT" \
        --config="$CLOUDRUN_CB_CONFIG" \
        --ignore-file="$CLOUDRUN_IGNORE" \
        "$SCION_REPO"
    else
      echo "Skipping existing ${IMAGE_REGISTRY}/hub:${IMAGE_TAG}"
    fi
  else
    echo "Plan: would build ${IMAGE_REGISTRY}/hub:${IMAGE_TAG} via Cloud Build"
  fi
fi

if [[ "$DRY_RUN" == "0" && "$APPROVE_APPLY" == 1 && "$IMAGE_PLATFORMS" != "all" && -d "${SCION_REPO}/image-build" ]]; then
  # build-images.sh ignores --platform under --builder cloud-build (the configs hardcode both platforms).
  # Base images use image-build/cloudbuild*.yaml; a single harness target uses harnesses/<name>/cloudbuild.yaml
  # or the config generated by builders/cloud-build.sh. All three must agree, or the harness build fails
  # looking for an arm64 scion-base that was never built.
  python3 - "${SCION_REPO}" "$IMAGE_PLATFORMS" <<'PY'
import pathlib, sys
root, platforms = pathlib.Path(sys.argv[1]), sys.argv[2]
targets = [
    *root.glob("image-build/cloudbuild*.yaml"),
    *root.glob("harnesses/*/cloudbuild*.yaml"),
    root / "image-build" / "scripts" / "builders" / "cloud-build.sh",
]
for p in sorted(t for t in targets if t.is_file()):
    src = p.read_text()
    if "linux/amd64,linux/arm64" in src:
        p.write_text(src.replace("linux/amd64,linux/arm64", platforms))
PY
fi

for base_target in core-base scion-base; do
  if [[ "$DRY_RUN" == "1" || "$APPROVE_APPLY" == 1 ]]; then
    if ! image_exists_in_ar "${IMAGE_REGISTRY}/${base_target}:${IMAGE_TAG}"; then
      submit_and_wait "$SCION_REPO/image-build/scripts/build-images.sh" \
        --builder cloud-build --target "$base_target" --registry "$IMAGE_REGISTRY" --tag "$IMAGE_TAG"
    else
      echo "Skipping existing ${IMAGE_REGISTRY}/${base_target}:${IMAGE_TAG}"
    fi
  else
    echo "Plan: would build ${IMAGE_REGISTRY}/${base_target}:${IMAGE_TAG}"
  fi
done

IFS=',' read -ra H_ARR <<< "$HARNESSES"
for raw_h in "${H_ARR[@]}"; do
  [[ -z "$raw_h" ]] && continue
  if [[ "$raw_h" == "all" || "$raw_h" == "harnesses" ]]; then
    if [[ "$DRY_RUN" == "1" || "$APPROVE_APPLY" == 1 ]]; then
      submit_and_wait "$SCION_REPO/image-build/scripts/build-images.sh" \
        --builder cloud-build --target harnesses --registry "$IMAGE_REGISTRY" --tag "$IMAGE_TAG"
    fi
  else
    step_target="$(resolve_harness_step "$raw_h")"
    if [[ "$DRY_RUN" == "1" || "$APPROVE_APPLY" == 1 ]]; then
      if ! image_exists_in_ar "${IMAGE_REGISTRY}/${step_target}:${IMAGE_TAG}"; then
        SCION_BASE_JUST_BUILT=1 submit_and_wait "$SCION_REPO/image-build/scripts/build-images.sh" \
          --builder cloud-build --target "$step_target" --registry "$IMAGE_REGISTRY" --tag "$IMAGE_TAG"
      else
        echo "Skipping existing ${IMAGE_REGISTRY}/${step_target}:${IMAGE_TAG}"
      fi
      run gcloud artifacts docker tags add \
        "${IMAGE_REGISTRY}/${step_target}:${IMAGE_TAG}" \
        "${IMAGE_REGISTRY}/${step_target}:latest" --quiet
    else
      echo "Plan: would build ${IMAGE_REGISTRY}/${step_target}:${IMAGE_TAG} and tag :latest"
    fi
  fi
done

HUB_URL_OUT="${HUB_URL:-https://...}"
IAP_CLIENT_ID="${IAP_OAUTH_CLIENT_ID:-}"
TARGET_PROJECT="${SANDBOX_PROJECT:-$HUB_PROJECT}"
TARGET_LOCATION="${VERTEX_LOCATION:-${GOOGLE_CLOUD_LOCATION:-global}}"

if [[ "$TIER" == "terraform-ha" ]]; then
  echo "== 4. Terraform HA: ${HUB_TF_DIR} (${HUB_NAME})"
  if [[ -z "$IAP_CLIENT_ID" && "$DRY_RUN" == "0" ]]; then
    EXISTING_HUB_URL=""
    if [[ "$HUB_MODE" == "cloudrun" ]]; then
      EXISTING_HUB_URL="$(discover_cloudrun_url)"
    fi
    IAP_CLIENT_ID="$(discover_iap_client_id "$EXISTING_HUB_URL")"
  fi
  write_hub_tfvars "${ABS_WORK_DIR}/hub.tfvars" "$IAP_CLIENT_ID"

  HUB_TF_PATH="${SCION_REPO}/deploy/terraform/configurations/${HUB_TF_DIR}"
  run terraform -chdir="${HUB_TF_PATH}" init -reconfigure \
    -backend-config="bucket=${STATE_BUCKET}" \
    -backend-config="prefix=${SHARED_PREFIX}/hubs/${HUB_NAME}"
  run terraform -chdir="${HUB_TF_PATH}" plan -var-file="${ABS_WORK_DIR}/hub.tfvars"
  if [[ "$APPROVE_APPLY" == 1 && "$DRY_RUN" == 0 ]]; then
    pass1_rc=0
    if [[ -z "$IAP_CLIENT_ID" && "$HUB_MODE" == "cloudrun" ]]; then
      set +e
      terraform -chdir="${HUB_TF_PATH}" apply -auto-approve -var-file="${ABS_WORK_DIR}/hub.tfvars"
      pass1_rc=$?
      set -e
    else
      run terraform -chdir="${HUB_TF_PATH}" apply -auto-approve -var-file="${ABS_WORK_DIR}/hub.tfvars"
    fi
    HUB_URL_OUT="$(terraform -chdir="${HUB_TF_PATH}" output -raw hub_url 2>/dev/null || terraform -chdir="${HUB_TF_PATH}" output -raw service_uri 2>/dev/null || echo "")"
    if [[ "$HUB_MODE" == "cloudrun" ]]; then
      DISCOVERED_CR_URL="$(discover_cloudrun_url)"
      HUB_URL_OUT="${DISCOVERED_CR_URL:-$HUB_URL_OUT}"
    fi
    if [[ -z "$IAP_CLIENT_ID" ]]; then
      for _iap_try in 1 2 3; do
        IAP_CLIENT_ID="$(discover_iap_client_id "$HUB_URL_OUT")"
        [[ -n "$IAP_CLIENT_ID" || -z "$HUB_URL_OUT" ]] && break
        sleep "${IAP_DISCOVERY_POLL_SEC:-5}"
      done
      if [[ -n "$IAP_CLIENT_ID" ]]; then
        echo "Discovered IAP OAuth client ID (${IAP_CLIENT_ID}); updating ${HUB_TF_DIR} transport audience..."
        write_hub_tfvars "${ABS_WORK_DIR}/hub.tfvars" "$IAP_CLIENT_ID"
        run terraform -chdir="${HUB_TF_PATH}" plan -var-file="${ABS_WORK_DIR}/hub.tfvars"
        run terraform -chdir="${HUB_TF_PATH}" apply -auto-approve -var-file="${ABS_WORK_DIR}/hub.tfvars"
        HUB_URL_PASS2="$(terraform -chdir="${HUB_TF_PATH}" output -raw hub_url 2>/dev/null || terraform -chdir="${HUB_TF_PATH}" output -raw service_uri 2>/dev/null || echo "")"
        if [[ "$HUB_MODE" == "cloudrun" ]]; then
          DISCOVERED_CR_URL="$(discover_cloudrun_url)"
          HUB_URL_PASS2="${DISCOVERED_CR_URL:-$HUB_URL_PASS2}"
        fi
        HUB_URL_OUT="${HUB_URL_PASS2:-$HUB_URL_OUT}"
      elif [[ $pass1_rc -ne 0 ]]; then
        echo "Pass-1 Terraform apply failed and no IAP OAuth client ID could be discovered." >&2
        exit "$pass1_rc"
      fi
    elif [[ $pass1_rc -ne 0 ]]; then
      exit "$pass1_rc"
    fi
  fi

  echo "== 5. Verify GKE Agent Cluster & Hub-Scope Defaults"
  run gcloud container clusters get-credentials "$CLUSTER_NAME" --project="$HUB_GKE_PROJECT" --region="$HUB_GKE_REGION"
  if [[ "$DRY_RUN" == "1" ]] || command -v kubectl >/dev/null 2>&1; then
    run kubectl get namespace "$HUB_K8S_NAMESPACE"
  else
    echo "Skipping local kubectl check (kubectl not found; namespace verified by Terraform)."
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "Plan: would set hub-scope env GOOGLE_CLOUD_PROJECT=${TARGET_PROJECT}, GOOGLE_CLOUD_REGION=${TARGET_LOCATION}, GOOGLE_CLOUD_LOCATION=${TARGET_LOCATION}"
  elif [[ "$APPROVE_APPLY" == 1 && -n "$HUB_URL_OUT" && "$HUB_URL_OUT" == https://* ]]; then
    HUB_URL="${HUB_API_BASE_URL:-$HUB_URL_OUT}" \
    HUB_IAP_AUDIENCE="$IAP_CLIENT_ID" \
    HUB_PROJECT="$HUB_PROJECT" \
    HUB_IMPERSONATE_SA="${HUB_IMPERSONATE_SA:-}" \
    _HUB_SCRIPT_DIR="$SCRIPT_DIR" \
    _HUB_TARGET_PROJECT="$TARGET_PROJECT" \
    _HUB_TARGET_LOCATION="$TARGET_LOCATION" \
    _HUB_URL_OUT="$HUB_URL_OUT" \
    _HUB_ENV_MAX_ATTEMPTS="${HUB_ENV_MAX_ATTEMPTS:-12}" \
    _HUB_ENV_RETRY_SEC="${HUB_ENV_RETRY_SEC:-10}" \
    python3 - <<'PY' || echo "Warning: could not set hub-scope env automatically; set via Hub API after IAP propagates." >&2
import os, sys, time, urllib.error
sys.path.insert(0, os.environ["_HUB_SCRIPT_DIR"])
from hub_client import HubAPIError, HubClient

target_project = os.environ["_HUB_TARGET_PROJECT"]
target_location = os.environ["_HUB_TARGET_LOCATION"]
max_attempts = max(int(os.environ.get("_HUB_ENV_MAX_ATTEMPTS", "12")), 1)
retry_sec = float(os.environ.get("_HUB_ENV_RETRY_SEC", "10"))

for attempt in range(1, max_attempts + 1):
    try:
        client = HubClient.from_env()
        for key, val in [
            ("GOOGLE_CLOUD_PROJECT", target_project),
            ("GOOGLE_CLOUD_REGION", target_location),
            ("GOOGLE_CLOUD_LOCATION", target_location),
        ]:
            # The Hub env API rejects SCION_* names (reserved control-plane namespace); cloud telemetry is
            # configured through Hub server-config settings instead.
            client.request("PUT", f"/api/v1/env/{key}", body={"value": val, "scope": "hub", "injectionMode": "always"}, timeout=15)
        client.request(
            "PUT",
            "/api/v1/admin/server-config",
            body={
                "telemetry": {
                    "enabled": True,
                    "cloud": {
                        "enabled": True,
                        "provider": "gcp",
                        "gcp_project_id": target_project,
                    },
                }
            },
            timeout=15,
        )
        print("Configured hub-scope Vertex AI and Cloud Telemetry defaults on", os.environ["_HUB_URL_OUT"])
        sys.exit(0)
    except HubAPIError as exc:
        if exc.status in (401, 403, 500, 502, 503, 504) and attempt < max_attempts:
            time.sleep(retry_sec)
            continue
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError, OSError):
        if attempt < max_attempts:
            time.sleep(retry_sec)
            continue
        sys.exit(1)
PY
  fi

elif [[ "$TIER" == "hybrid-gke" ]]; then
  echo "== 4. Hybrid GKE provisioning"
  cat > "${ABS_WORK_DIR}/deploy-config.json" <<EOF
{
  "gke_target": {
    "cluster_name": "${CLUSTER_NAME}",
    "location": "${HUB_GKE_REGION}",
    "namespace": "${HUB_K8S_NAMESPACE}"
  },
  "container_images": {
    "source": "registry",
    "registry": "${IMAGE_REGISTRY}",
    "tag": "${IMAGE_TAG}"
  }
}
EOF
  if [[ "$APPROVE_APPLY" == 1 && "$DRY_RUN" == 0 ]]; then
    run "$SCION_REPO/scripts/single-node-vm/deploy.sh" --config "${ABS_WORK_DIR}/deploy-config.json"
  else
    echo "Plan: would run $SCION_REPO/scripts/single-node-vm/deploy.sh --config ${ABS_WORK_DIR}/deploy-config.json"
  fi
fi

if [[ ${#POST_HOOKS[@]} -gt 0 ]]; then
  run_hooks "post" "${POST_HOOKS[@]}"
fi

echo ""
echo "=== factory.env additions ==="
echo "HUB_PROJECT=${HUB_PROJECT}"
echo "SANDBOX_PROJECT=${TARGET_PROJECT}"
echo "HUB_URL=${HUB_URL_OUT:-https://...}"
echo "HUB_NAME=${HUB_NAME}"
echo "HUB_REGION=${HUB_REGION}"
if [[ -n "$IAP_CLIENT_ID" ]]; then
  echo "HUB_IAP_AUDIENCE=${IAP_CLIENT_ID}"
fi
if [[ "${USE_OPERATOR_SA:-0}" == "1" && -n "${HUB_IMPERSONATE_SA:-}" ]]; then
  echo "HUB_IMPERSONATE_SA=${HUB_IMPERSONATE_SA}"
fi
echo "AGENT_RUNTIME=kubernetes"
echo "RUNTIME_PROFILE=${RUNTIME_PROFILE:-remote}"
if [[ "$HUB_GKE_PROJECT" != "$HUB_PROJECT" ]]; then
  echo "HUB_GKE_PROJECT=${HUB_GKE_PROJECT}"
fi
if [[ "$HUB_GKE_REGION" != "$HUB_REGION" ]]; then
  echo "HUB_GKE_REGION=${HUB_GKE_REGION}"
fi
echo "HUB_K8S_CLUSTER=${CLUSTER_NAME}"
echo "HUB_K8S_NAMESPACE=${HUB_K8S_NAMESPACE}"
echo "IMAGE_REGISTRY=${IMAGE_REGISTRY}"
echo "VERTEX_LOCATION=${TARGET_LOCATION}"

if [[ -n "$WRITE_ENV_FILE" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ write-env %s (chmod 600)\n' "$WRITE_ENV_FILE" >&2
  else
    _WE_HUB_PROJECT="$HUB_PROJECT" \
    _WE_SANDBOX_PROJECT="${SANDBOX_PROJECT:-}" \
    _WE_SANDBOX_REGION="${SANDBOX_REGION:-}" \
    _WE_FIRESTORE_PROJECT="${FIRESTORE_PROJECT:-}" \
    _WE_DATA_BUCKET="${DATA_BUCKET:-}" \
    _WE_HUB_URL="${HUB_URL_OUT:-}" \
    _WE_HUB_NAME="$HUB_NAME" \
    _WE_HUB_REGION="$HUB_REGION" \
    _WE_HUB_IAP_AUDIENCE="${IAP_CLIENT_ID:-}" \
    _WE_USE_OPERATOR_SA="${USE_OPERATOR_SA:-0}" \
    _WE_HUB_IMPERSONATE_SA="${HUB_IMPERSONATE_SA:-}" \
    _WE_HUB_K8S_CLUSTER="$CLUSTER_NAME" \
    _WE_HUB_K8S_NAMESPACE="$HUB_K8S_NAMESPACE" \
    _WE_IMAGE_REGISTRY="$IMAGE_REGISTRY" \
    _WE_VERTEX_LOCATION="$TARGET_LOCATION" \
    python3 - "$WRITE_ENV_FILE" <<'PY'
import os
import pathlib
import re
import sys
import tempfile

target = pathlib.Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)

existing_lines = target.read_text().splitlines() if target.exists() else []
assign_re = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

existing_vals = {}
for line in existing_lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    m = assign_re.match(line)
    if m:
        existing_vals[m.group(2)] = m.group(3).strip().strip("'\"")

hub_project = os.environ["_WE_HUB_PROJECT"]
hub_region = os.environ["_WE_HUB_REGION"]
sandbox_project = os.environ.get("_WE_SANDBOX_PROJECT") or existing_vals.get("SANDBOX_PROJECT") or hub_project
sandbox_region = os.environ.get("_WE_SANDBOX_REGION") or existing_vals.get("SANDBOX_REGION") or hub_region
firestore_project = os.environ.get("_WE_FIRESTORE_PROJECT") or existing_vals.get("FIRESTORE_PROJECT") or sandbox_project
data_bucket = os.environ.get("_WE_DATA_BUCKET") or existing_vals.get("DATA_BUCKET") or f"{sandbox_project}-factory-data"

ordered_updates = [
    ("HUB_PROJECT", hub_project),
    ("SANDBOX_PROJECT", sandbox_project),
    ("SANDBOX_REGION", sandbox_region),
    ("FIRESTORE_PROJECT", firestore_project),
    ("DATA_BUCKET", data_bucket),
]

hub_url = os.environ.get("_WE_HUB_URL", "")
if hub_url and hub_url != "https://...":
    ordered_updates.append(("HUB_URL", hub_url))

ordered_updates.extend([
    ("HUB_NAME", os.environ["_WE_HUB_NAME"]),
    ("HUB_REGION", hub_region),
])

iap_aud = os.environ.get("_WE_HUB_IAP_AUDIENCE", "")
if iap_aud:
    ordered_updates.append(("HUB_IAP_AUDIENCE", iap_aud))

if os.environ.get("_WE_USE_OPERATOR_SA") == "1" and os.environ.get("_WE_HUB_IMPERSONATE_SA"):
    ordered_updates.append(("HUB_IMPERSONATE_SA", os.environ["_WE_HUB_IMPERSONATE_SA"]))

ordered_updates.extend([
    ("AGENT_RUNTIME", "kubernetes"),
    ("RUNTIME_PROFILE", "remote"),
    ("HUB_K8S_CLUSTER", os.environ["_WE_HUB_K8S_CLUSTER"]),
    ("HUB_K8S_NAMESPACE", os.environ["_WE_HUB_K8S_NAMESPACE"]),
    ("IMAGE_REGISTRY", os.environ["_WE_IMAGE_REGISTRY"]),
    ("VERTEX_LOCATION", os.environ["_WE_VERTEX_LOCATION"]),
])

updates_map = dict(ordered_updates)
seen = set()
out_lines = []

for line in existing_lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        out_lines.append(line)
        continue
    m = assign_re.match(line)
    if not m:
        out_lines.append(line)
        continue
    prefix, key = m.group(1), m.group(2)
    if key in updates_map:
        if key not in seen:
            out_lines.append(f"{prefix}{key}={updates_map[key]}")
            seen.add(key)
    else:
        out_lines.append(line)

for key, val in ordered_updates:
    if key not in seen:
        out_lines.append(f"{key}={val}")
        seen.add(key)

fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, target)
    os.chmod(target, 0o600)
except Exception:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)
    raise
PY
  fi
fi
