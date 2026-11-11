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

# Ensure Vertex AI / Google Cloud SDK quota billing stays pinned to the agent's target project
# rather than inheriting a shared GKE Autopilot host cluster project.
if [ -n "${GOOGLE_CLOUD_PROJECT:-}" ] && [ -z "${GOOGLE_CLOUD_QUOTA_PROJECT:-}" ]; then
  export GOOGLE_CLOUD_QUOTA_PROJECT="${GOOGLE_CLOUD_PROJECT}"
fi

# Google-managed Cloud Run Direct IAP (369001918367-*) rejects OIDC ID tokens unless a custom OAuth client is
# manually bound via Console. sciontool also validates JWT `exp` claims on SCION_TRANSPORT_TOKEN_FILE, so when
# SCION_HUB_ENDPOINT is HTTPS we prefer minting a short-lived RS256 JWT via IAM `signJwt` (`aud: https://<host>/*`),
# and gracefully fall back to the metadata SA OAuth2 access token when `signJwt` is unavailable.
if [ "${SCION_TRANSPORT_MODE:-}" = "iap" ]; then
  case "${SCION_TRANSPORT_AUDIENCE:-}" in
    369001918367-*|direct|https://*)
      _iap_tok_file="${HOME}/.scion/iap-access-token"
      _refresh_direct_iap_token() {
        command -v python3 >/dev/null 2>&1 || return 1
        SCION_IAP_TOK_OUT="$_iap_tok_file" python3 - <<'PY' >/dev/null 2>&1
import json, os, time, urllib.parse, urllib.request
out = os.environ["SCION_IAP_TOK_OUT"]
os.makedirs(os.path.dirname(out), exist_ok=True)
bases = [os.environ.get("SCION_METADATA_URL", "").rstrip("/"), "http://169.254.169.254", "http://metadata.google.internal"]
scopes = "https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid"
hub_url = (os.environ.get("SCION_HUB_ENDPOINT") or os.environ.get("HUB_URL") or "").strip()
aud_env = (os.environ.get("SCION_TRANSPORT_AUDIENCE") or "").strip()
jwt_aud = ""
if aud_env.startswith("https://"):
    jwt_aud = aud_env if aud_env.endswith("/*") else aud_env.rstrip("/") + "/*"
elif hub_url.startswith("https://"):
    p = urllib.parse.urlparse(hub_url)
    if p.netloc:
        jwt_aud = f"https://{p.netloc}/*"

def _write_atomic(tok: str) -> None:
    tmp = f"{out}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.replace(tmp, out)

for base in [b for b in bases if b]:
    for _ in range(2):
        try:
            req = urllib.request.Request(
                f"{base}/computeMetadata/v1/instance/service-accounts/default/token?scopes={scopes}",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                access_tok = (json.loads(resp.read().decode("utf-8", errors="replace")).get("access_token") or "").strip()
            if not access_tok:
                continue
            if jwt_aud:
                try:
                    er = urllib.request.Request(
                        f"{base}/computeMetadata/v1/instance/service-accounts/default/email",
                        headers={"Metadata-Flavor": "Google"},
                    )
                    with urllib.request.urlopen(er, timeout=5.0) as eresp:
                        sa_email = eresp.read().decode("utf-8", errors="replace").strip()
                    if "@" in sa_email:
                        now = int(time.time())
                        payload = json.dumps({"iss": sa_email, "sub": sa_email, "aud": jwt_aud, "iat": now, "exp": now + 3600})
                        iam_base = (os.environ.get("SCION_IAMCREDENTIALS_URL") or "https://iamcredentials.googleapis.com").rstrip("/")
                        sreq = urllib.request.Request(
                            f"{iam_base}/v1/projects/-/serviceAccounts/{urllib.parse.quote(sa_email, safe='@.')}:signJwt",
                            data=json.dumps({"payload": payload}).encode("utf-8"),
                            headers={"Authorization": f"Bearer {access_tok}", "Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(sreq, timeout=10.0) as sresp:
                            signed = (json.loads(sresp.read().decode("utf-8", errors="replace")).get("signedJwt") or "").strip()
                        if signed:
                            _write_atomic(signed)
                            raise SystemExit(0)
                except SystemExit:
                    raise
                except Exception:
                    pass
            _write_atomic(access_tok)
            raise SystemExit(0)
        except SystemExit:
            raise
        except Exception:
            pass
raise SystemExit(1)
PY
      }
      if _refresh_direct_iap_token && [ -s "$_iap_tok_file" ]; then
        export SCION_TRANSPORT_TOKEN_FILE="$_iap_tok_file"
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

sub="${FACTORY_SUBDIR:-}"
sub="${sub#./}"
sub="${sub%/}"
if [ -z "$sub" ] || [ "$sub" = "." ]; then
  exec "$@"
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

exec "$@"
