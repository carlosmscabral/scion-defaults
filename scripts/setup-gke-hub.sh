#!/usr/bin/env bash
set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TIER="terraform-ha"
HUB_MODE="cloudrun"
SCION_REPO=""
HARNESSES="claude,antigravity"
IMAGE_TAG="${IMAGE_TAG:-v$(date -u +%Y%m%d%H%M%S)}"
WORK_DIR="build/gke-hub"
APPROVE_APPLY=0
DRY_RUN="${DRY_RUN:-0}"
ENV_FILE=""
PRE_HOOKS=()
POST_HOOKS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --tier) TIER="$2"; shift 2 ;;
    --hub-mode) HUB_MODE="$2"; shift 2 ;;
    --scion-repo) SCION_REPO="$2"; shift 2 ;;
    --harnesses) HARNESSES="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --pre-hook) PRE_HOOKS+=("$2"); shift 2 ;;
    --post-hook) POST_HOOKS+=("$2"); shift 2 ;;
    --approve-apply) APPROVE_APPLY=1; shift 1 ;;
    --dry-run) DRY_RUN=1; shift 1 ;;
    -h|--help) echo "Usage: $0 [options]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HUB_PROJECT="${HUB_PROJECT:-}"
SHARED_PREFIX="${SHARED_PREFIX:-tfha}"
HUB_NAME="${HUB_NAME:-prod}"
PO_EMAIL="${PO_EMAIL:-}"

if [[ -z "$HUB_PROJECT" || -z "$PO_EMAIL" ]]; then
  echo "HUB_PROJECT and PO_EMAIL must be set." >&2
  exit 1
fi

HUB_REGION="${HUB_REGION:-${SANDBOX_REGION:-us-central1}}"
HUB_ZONE="${HUB_ZONE:-${HUB_REGION}-b}"
SQL_AVAILABILITY_TYPE="${SQL_AVAILABILITY_TYPE:-REGIONAL}"
STATE_BUCKET="${STATE_BUCKET:-${HUB_PROJECT}-${SHARED_PREFIX}-tfstate}"
BUILD_POLL_SEC="${BUILD_POLL_SEC:-10}"
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
  CLUSTER_NAME="${SHARED_PREFIX}-agents"
  HUB_K8S_NAMESPACE="${HUB_NAME}"
  IMAGE_REGISTRY="${IMAGE_REGISTRY:-${HUB_REGION}-docker.pkg.dev/${HUB_PROJECT}/${SHARED_PREFIX}-scion}"
else
  CLUSTER_NAME="scion-hub-${HUB_NAME}"
  HUB_K8S_NAMESPACE="scion-hub-${HUB_NAME}"
  IMAGE_REGISTRY="${IMAGE_REGISTRY:-${HUB_REGION}-docker.pkg.dev/${HUB_PROJECT}/scion}"
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
  if [[ "$h" == "gemini" && ! -d "${SCION_REPO}/harnesses/gemini" && -d "${SCION_REPO}/harnesses/gemini-cli" ]]; then
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
}

if [[ -z "$SCION_REPO" ]]; then
  echo "--scion-repo is required to build images and run Terraform." >&2
  exit 1
fi

if [[ -n "${PRE_HOOKS_ENV:-}" ]]; then
  IFS=',' read -ra _ENV_PRE <<< "$PRE_HOOKS_ENV"
  PRE_HOOKS=("${_ENV_PRE[@]}" "${PRE_HOOKS[@]}")
fi
if [[ -n "${POST_HOOKS_ENV:-}" ]]; then
  IFS=',' read -ra _ENV_POST <<< "$POST_HOOKS_ENV"
  POST_HOOKS+=("${_ENV_POST[@]}")
fi
for _h in "${PRE_HOOKS[@]}" "${POST_HOOKS[@]}"; do
  [[ -n "$_h" ]] || continue
  if [[ ! -f "$_h" ]]; then
    echo "Hook script not found: ${_h}" >&2
    exit 1
  fi
done

mkdir -p "$WORK_DIR"
ABS_WORK_DIR="$(cd "$WORK_DIR" && pwd)"

if [[ ${#PRE_HOOKS[@]} -gt 0 ]]; then
  run_hooks "pre" "${PRE_HOOKS[@]}"
fi

echo "== 1. Enable APIs & Terraform State Bucket"
run gcloud services enable --project="$HUB_PROJECT" \
  compute.googleapis.com servicenetworking.googleapis.com container.googleapis.com run.googleapis.com \
  file.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com iap.googleapis.com aiplatform.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com storage.googleapis.com

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

  echo "== 2. Terraform HA: shared-infra (${SHARED_PREFIX})"
  cat > "${ABS_WORK_DIR}/shared-infra.tfvars" <<EOF
project_id            = "${HUB_PROJECT}"
region                = "${HUB_REGION}"
zone                  = "${HUB_ZONE}"
name_prefix           = "${SHARED_PREFIX}"
sql_availability_type = "${SQL_AVAILABILITY_TYPE}"
EOF

  SHARED_TF_PATH="${SCION_REPO}/deploy/terraform/configurations/shared-infra"
  run terraform -chdir="${SHARED_TF_PATH}" init -reconfigure \
    -backend-config="bucket=${STATE_BUCKET}" \
    -backend-config="prefix=${SHARED_PREFIX}/shared-infra"
  run terraform -chdir="${SHARED_TF_PATH}" plan -var-file="${ABS_WORK_DIR}/shared-infra.tfvars"
  if [[ "$APPROVE_APPLY" == 1 && "$DRY_RUN" == 0 ]]; then
    run terraform -chdir="${SHARED_TF_PATH}" apply -auto-approve -var-file="${ABS_WORK_DIR}/shared-infra.tfvars"
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
      gcloud projects add-iam-policy-binding "$HUB_PROJECT" \
        --member="serviceAccount:${CB_SA}" --role="roles/cloudbuild.builds.builder" --condition=None >/dev/null 2>&1 || \
      gcloud projects add-iam-policy-binding "$HUB_PROJECT" \
        --member="serviceAccount:${CB_SA}" --role="roles/cloudbuild.builds.builder" >/dev/null
      gcloud artifacts repositories add-iam-policy-binding "$REPO_NAME" \
        --project="$HUB_PROJECT" --location="$REPO_LOC" \
        --member="serviceAccount:${CB_SA}" --role="roles/artifactregistry.writer" >/dev/null
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
    python3 - "${SCION_REPO}/pkg/runtime/k8s_runtime.go" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
pat = re.compile(
    r'(if config\.SharedDirStorage\.Shared\(\) \{[\s\S]*?PersistentVolumeClaim:\s*&corev1\.PersistentVolumeClaimVolumeSource\{\n'
    r'[ \t]*ClaimName:\s*claim,\n)'
    r'[ \t]*ReadOnly:\s*sd\.ReadOnly,\n'
)
updated, count = pat.subn(r'\1', src, count=1)
if count == 1:
    p.write_text(updated)
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
        submit_and_wait "$SCION_REPO/image-build/scripts/build-images.sh" \
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
  run gcloud container clusters get-credentials "$CLUSTER_NAME" --project="$HUB_PROJECT" --region="$HUB_REGION"
  run kubectl get namespace "$HUB_K8S_NAMESPACE"

  TARGET_PROJECT="${SANDBOX_PROJECT:-$HUB_PROJECT}"
  TARGET_LOCATION="${VERTEX_LOCATION:-${GOOGLE_CLOUD_LOCATION:-global}}"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "Plan: would set hub-scope env GOOGLE_CLOUD_PROJECT=${TARGET_PROJECT}, GOOGLE_CLOUD_REGION=${TARGET_LOCATION}, GOOGLE_CLOUD_LOCATION=${TARGET_LOCATION}"
  elif [[ "$APPROVE_APPLY" == 1 && -n "$HUB_URL_OUT" && "$HUB_URL_OUT" == https://* ]]; then
    HUB_URL="$HUB_URL_OUT" HUB_IAP_AUDIENCE="$IAP_CLIENT_ID" HUB_PROJECT="$HUB_PROJECT" HUB_IMPERSONATE_SA="${HUB_IMPERSONATE_SA:-}" python3 - <<PY || echo "Warning: could not set hub-scope env automatically; set via Hub API after IAP propagates." >&2
import os, sys
sys.path.insert(0, "${SCRIPT_DIR}")
from hub_client import HubClient
client = HubClient.from_env()
for key, val in [
    ("GOOGLE_CLOUD_PROJECT", "${TARGET_PROJECT}"),
    ("GOOGLE_CLOUD_REGION", "${TARGET_LOCATION}"),
    ("GOOGLE_CLOUD_LOCATION", "${TARGET_LOCATION}"),
]:
    client.request("PUT", f"/api/v1/env/{key}", body={"value": val, "scope": "hub", "injectionMode": "always"}, timeout=15)
print("Configured hub-scope Vertex AI defaults on", "${HUB_URL_OUT}")
PY
  fi

elif [[ "$TIER" == "hybrid-gke" ]]; then
  echo "== 4. Hybrid GKE provisioning"
  cat > "${ABS_WORK_DIR}/deploy-config.json" <<EOF
{
  "gke_target": {
    "cluster_name": "${CLUSTER_NAME}",
    "location": "${HUB_REGION}",
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
echo "HUB_URL=${HUB_URL_OUT:-https://...}"
echo "HUB_NAME=${HUB_NAME}"
echo "HUB_REGION=${HUB_REGION}"
if [[ -n "$IAP_CLIENT_ID" ]]; then
  echo "HUB_IAP_AUDIENCE=${IAP_CLIENT_ID}"
fi
echo "AGENT_RUNTIME=kubernetes"
echo "RUNTIME_PROFILE=remote"
echo "HUB_K8S_CLUSTER=${CLUSTER_NAME}"
echo "HUB_K8S_NAMESPACE=${HUB_K8S_NAMESPACE}"
echo "IMAGE_REGISTRY=${IMAGE_REGISTRY}"
