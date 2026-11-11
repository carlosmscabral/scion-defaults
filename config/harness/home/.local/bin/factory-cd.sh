#!/bin/sh
# factory-cd.sh — start the harness in the factory's product folder of a monorepo.
#
# Installed into the home/ of the Hub's harness-configs and put in front of their command.base by
# scripts/patch-harness-start-dir.py, so the harness command becomes:
#     sh /home/scion/.local/bin/factory-cd.sh <original command.base...> <scion args...>
#
# FACTORY_SUBDIR (set by the rendered template's env, or a project-scoped Hub env var) is the product folder
# relative to the repository root, e.g. src/project1. Unset, empty or "." runs the harness unchanged, so the patched
# harness-configs stay safe for every other project on the Hub.
#
# The agent still has the whole repository; only the folder the harness starts in changes. Scion has no
# working-directory setting of its own (the Docker runtime always uses --workdir /workspace or the worktree), see
# docs/plans/2026-10-22-monorepo-factories.md.
#
# Exit codes: 64 FACTORY_SUBDIR is absolute or leaves the repository; 66 the folder does not exist.

# Google-managed Cloud Run Direct IAP (369001918367-*) rejects OIDC ID tokens unless a custom OAuth client is
# manually bound via Console, and instead accepts Service Account OAuth2 access tokens in Proxy-Authorization.
# When running under Direct IAP, swap SCION_TRANSPORT_TOKEN_FILE to a metadata-fetched SA access token (refreshed
# every 25m) and unset SCION_TRANSPORT_TOKEN so `scion list|start|resume|message` works zero-click inside pods.
if [ "${SCION_TRANSPORT_MODE:-}" = "iap" ]; then
  case "${SCION_TRANSPORT_AUDIENCE:-}" in
    369001918367-*)
      _refresh_direct_iap_token() {
        if [ -x "${HOME}/.scion/hooks/pre-start.d/10-direct-iap-token" ]; then
          "${HOME}/.scion/hooks/pre-start.d/10-direct-iap-token" >/dev/null 2>&1
        else
          command -v python3 >/dev/null 2>&1 || return 1
          python3 - <<'PY' >/dev/null 2>&1
import json, os, urllib.request, time
def _write_atomic(path, content):
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.replace(tmp, path)
home = os.environ.get("HOME", "")
if not home: raise SystemExit(1)
out1 = os.path.join(home, ".scion", "transport-token")
out2 = os.path.join(home, ".scion", "iap-access-token")
os.makedirs(os.path.dirname(out1), exist_ok=True)
meta_url = os.environ.get("SCION_METADATA_URL", "").rstrip("/")
fallbacks = os.environ.get("SCION_METADATA_FALLBACKS", "")
if fallbacks == "0":
    bases = [meta_url] if meta_url else []
else:
    bases = [meta_url, "http://169.254.169.254", "http://metadata.google.internal"]
bases = [b for b in bases if b]
scopes = "https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid"
sleep_sec = float(os.environ.get("SCION_IAP_RETRY_SLEEP_SEC", "1.0"))
for base in bases:
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{base}/computeMetadata/v1/instance/service-accounts/default/token?scopes={scopes}",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                tok = (json.loads(resp.read().decode("utf-8", errors="replace")).get("access_token") or "").strip()
            if tok:
                _write_atomic(out1, tok)
                _write_atomic(out2, tok)
                raise SystemExit(0)
        except SystemExit:
            raise
        except Exception:
            pass
        if attempt < 2:
            time.sleep(sleep_sec)
raise SystemExit(1)
PY
        fi
      }
      if _refresh_direct_iap_token && [ -s "${HOME}/.scion/iap-access-token" ]; then
        export SCION_TRANSPORT_TOKEN_FILE="${HOME}/.scion/iap-access-token"
        unset SCION_TRANSPORT_TOKEN
        _iap_refresh="${FACTORY_CD_IAP_REFRESH_SEC:-1500}"
        if [ "$_iap_refresh" -gt 0 ] 2>/dev/null; then
          _f_pid="$$"
          (
            while sleep "$_iap_refresh"; do
              kill -0 "$_f_pid" 2>/dev/null || exit 0
              _refresh_direct_iap_token || true
            done
          ) </dev/null >/dev/null 2>&1 &
        fi
      fi
      ;;
  esac
fi

# Persist container-local ~/.scion/agent.log and ~/.gemini/**/transcript_full.jsonl onto the durable scratchpad
# shared-dir (/scion-volumes/scratchpad/.telemetry/<agent_slug>/<pod_name>/) periodically and on EXIT/TERM so
# KubernetesRuntime.Stop pod deletion or preemption never loses telemetry.
_telem_scratch="${SCION_SCRATCHPAD_DIR:-/scion-volumes/scratchpad}"
_persist_telemetry() {
  [ -d "$_telem_scratch" ] && [ -n "${HOME:-}" ] || return 0
  if [ -x "${HOME}/.scion/hooks/pre-stop.d/10-persist-telemetry" ]; then
    SCION_SCRATCHPAD_DIR="$_telem_scratch" "${HOME}/.scion/hooks/pre-stop.d/10-persist-telemetry" >/dev/null 2>&1 || true
    return 0
  fi
  _ag_slug="${SCION_AGENT_SLUG:-${SCION_AGENT_NAME:-unknown}}"
  _pod_nm="${HOSTNAME:-$(hostname 2>/dev/null || echo pod)}"
  [ -n "$_pod_nm" ] || _pod_nm="pod"
  _dst_root="${_telem_scratch}/.telemetry/${_ag_slug}/${_pod_nm}"
  _cp_telem() {
    [ -f "$1" ] || return 0
    mkdir -p "$(dirname "$2")" 2>/dev/null || return 0
    _t="${2}.$$.tmp"
    if cp "$1" "$_t" 2>/dev/null; then
      chmod 600 "$_t" 2>/dev/null || true
      mv -f "$_t" "$2" 2>/dev/null || rm -f "$_t" 2>/dev/null || true
    else
      rm -f "$_t" 2>/dev/null || true
    fi
  }
  # Current Scion writes ~/agent.log; older builds wrote ~/.scion/agent.log.
  if [ -f "${HOME}/agent.log" ]; then
    _cp_telem "${HOME}/agent.log" "${_dst_root}/agent.log"
  else
    _cp_telem "${HOME}/.scion/agent.log" "${_dst_root}/agent.log"
  fi
  if [ -d "${HOME}/.gemini" ]; then
    find "${HOME}/.gemini" -type f -name "transcript_full.jsonl" 2>/dev/null | while IFS= read -r _tf; do
      [ -n "$_tf" ] || continue
      _cp_telem "$_tf" "${_dst_root}/${_tf#"${HOME}"/}"
    done
  fi
}

_run_harness() {
  if [ ! -d "$_telem_scratch" ]; then
    exec "$@"
  fi
  _persist_telemetry
  _telem_bg_pid=""
  _telem_sync_sec="${FACTORY_CD_TELEMETRY_SYNC_SEC:-30}"
  if [ "$_telem_sync_sec" -gt 0 ] 2>/dev/null; then
    _h_pid="$$"
    (
      trap '_persist_telemetry; exit 0' TERM INT HUP
      while sleep "$_telem_sync_sec"; do
        kill -0 "$_h_pid" 2>/dev/null || { _persist_telemetry; exit 0; }
        _persist_telemetry
      done
    ) </dev/null >/dev/null 2>&1 &
    _telem_bg_pid="$!"
  fi
  trap '_rc=$?; [ -n "${_telem_bg_pid:-}" ] && kill "$_telem_bg_pid" 2>/dev/null || true; _persist_telemetry; exit $_rc' EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  "$@"
  exit $?
}

sub="${FACTORY_SUBDIR:-}"
sub="${sub#./}"
sub="${sub%/}"
if [ -z "$sub" ] || [ "$sub" = "." ]; then
  _run_harness "$@"
fi

case "/$sub/" in
  //*|*/../*)
    echo "factory-cd: FACTORY_SUBDIR '$FACTORY_SUBDIR' must be a folder inside the repository, relative to its root" >&2
    exit 64
    ;;
esac

root="$(git rev-parse --show-toplevel 2>/dev/null)" || root=""
[ -n "$root" ] || root="$(pwd -P)"
if [ ! -d "$root/$sub" ]; then
  echo "factory-cd: product folder $root/$sub (FACTORY_SUBDIR=$sub) does not exist in this checkout" >&2
  exit 66
fi
cd "$root/$sub" || exit 66
target="$(pwd -P)"

# Antigravity fires only project-local hooks (<workspace>/.agents/hooks.json, written by its provisioner at the
# repository root). Carry them into the product folder so Scion's status hooks keep firing, and keep the copy out
# of Git. The provisioner rewrites the root copy on every start, so refresh ours whenever it differs.
if [ -f "$root/.agents/hooks.json" ]; then
  if ! cmp -s "$root/.agents/hooks.json" "$target/.agents/hooks.json"; then
    mkdir -p "$target/.agents" && cp "$root/.agents/hooks.json" "$target/.agents/hooks.json"
  fi
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=""
  if [ -n "$common" ]; then
    case "$common" in /*) ;; *) common="$target/$common" ;; esac
    mkdir -p "$common/info"
    grep -qxF '.agents/' "$common/info/exclude" 2>/dev/null || echo '.agents/' >> "$common/info/exclude"
  fi
fi

# The provisioners pre-trust only /workspace; trust the product folder too so no trust prompt blocks the harness.
if command -v python3 >/dev/null 2>&1; then
  FACTORY_CD_TARGET="$target" python3 - <<'PY' || echo "factory-cd: could not pre-trust $target (continuing)" >&2
import json, os

target = os.environ["FACTORY_CD_TARGET"]
home = os.path.expanduser("~")


def update(path, change):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        data = json.load(f)
    if change(data):
        tmp = path + ".factory-cd.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def claude(data):
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(target, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return False
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("projectOnboardingSeenCount", 1)
    return True


def antigravity(data):
    trusted = data.get("trustedWorkspaces")
    if not isinstance(trusted, list):
        trusted = data["trustedWorkspaces"] = []
    if target in trusted:
        return False
    trusted.append(target)
    return True


update(os.path.join(home, ".claude.json"), claude)
update(os.path.join(home, ".gemini", "antigravity-cli", "settings.json"), antigravity)
PY
fi

_run_harness "$@"
