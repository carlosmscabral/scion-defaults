#!/usr/bin/env python3
"""Factory Watchdog daemon & circuit breaker for Scion Hub.

Runs periodically (every 5 min via cron) on the Hub VM as the Hub's OS user.

Checks performed:
  1. Antigravity provisioner drift guard:
     Checks ~/.scion/workspace-storage/hubs/*/harness-configs/global/antigravity/provision.py.
     If unpatched, runs patch-antigravity-hooks.py --apply so Hub service restarts
     (which run OverwriteBuiltinManaged) never leave Antigravity broken.
  2. Heartbeat schedule guard:
     Ensures project heartbeat schedules (e.g. *-lead-heartbeat) remain 'active' in hub.db.
  3. Agent liveness & circuit-breaker recovery:
     Monitors agent phases and docker containers for factory projects (psearch, psearch-eval).
     - If suspended: wakes via Hub API message with wake=True.
     - If stopped/error or container exited: restarts agent via Hub API /start (forceResume=True if error)
       and messages the project lead.
     - Enforces circuit breaker: max MAX_RESTARTS_PER_HOUR (default 3) per agent per hour.
       If exceeded, stops restart loops and logs an ERROR alert to Cloud Logging (scion-factory-alerts).
  4. PO Inbox escalation forwarder:
     Polls messages table for new messages addressed to user:<ADMIN_EMAIL>.
     Emits structured Cloud Logging event (PO_ESCALATION, severity ERROR) for Cloud Monitoring alerting.

Usage:
  python3 factory-watchdog.py                 # check only (dry-run)
  python3 factory-watchdog.py --apply         # execute recovery actions
  python3 factory-watchdog.py --install-cron  # install to ~/.scion and register in crontab
"""

import argparse
import base64
import datetime
import glob
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SCION_HOME = os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion"))
HUB_DB = os.path.join(SCION_HOME, "hub.db")
HUB_ENV = os.path.join(SCION_HOME, "hub.env")
SYNC_ENV = os.path.join(SCION_HOME, "sync.env")
WATCHDOG_ENV = os.path.join(SCION_HOME, "watchdog.env")
WATCHDOG_STATE = os.path.join(SCION_HOME, "watchdog-state.json")
PATCH_SCRIPT = os.path.join(SCION_HOME, "patch-antigravity-hooks.py")

HUB_LOCAL_URL = os.environ.get("HUB_LOCAL_URL", "http://localhost:8080").rstrip("/")

_TOKEN = None


def load_env_file(path, environ=None):
    target = os.environ if environ is None else environ
    if not path or not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("\"'")
            if k not in target:
                target[k] = v


def load_runtime_env(watchdog_env=WATCHDOG_ENV, sync_env=SYNC_ENV, environ=None):
    """Load watchdog.env first, then sync.env as fallback, without overriding explicit caller env vars."""
    target = os.environ if environ is None else environ
    load_env_file(watchdog_env, target)
    load_env_file(sync_env, target)


# Load environment files
load_runtime_env()

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL") or os.environ.get("PO_EMAIL", "")
HUB_PROJECT_ID = os.environ.get("HUB_PROJECT_ID") or os.environ.get("HUB_PROJECT", "")
def default_factory_projects(env):
    """FACTORY_PROJECTS, else BUILD_PROJECT and EVAL_PROJECT (run-on-hub.sh forwards both), else the psearch reference."""
    projects = [p.strip() for p in env.get("FACTORY_PROJECTS", "").split(",") if p.strip()]
    projects = projects or [p for p in (env.get("BUILD_PROJECT"), env.get("EVAL_PROJECT")) if p]
    return projects or ["psearch", "psearch-eval"]


FACTORY_PROJECTS = default_factory_projects(os.environ)
MAX_RESTARTS_PER_HOUR = int(os.environ.get("MAX_RESTARTS_PER_HOUR", "3"))
WATCHDOG_SUBPROCESS_TIMEOUT_SEC = int(os.environ.get("WATCHDOG_SUBPROCESS_TIMEOUT_SEC", "600"))

_HARNESS_COMMS = {
    "sciontool",
    "tmux",
    "tmux: client",
    "tmux: server",
    "sh",
    "bash",
    "zsh",
    "-zsh",
    "-bash",
    "agy",
    "claude",
    "opencode",
    "hermes",
    "ps",
    "sleep",
}

_HARNESS_ARG_PREFIXES = (
    "sciontool ",
    "tmux new-session",
    "sh -c tmux new-session",
    "sh -c '/home/scion/.scion/harness/",
    "/home/scion/.scion/harness/",
    "agy ",
    "claude ",
    "opencode ",
    "hermes ",
    "ps -eo ",
)


def find_hung_subprocesses(ps_output: str, max_age_sec: int = WATCHDOG_SUBPROCESS_TIMEOUT_SEC) -> list[dict]:
    """Return non-harness child commands inside an agent container that have run for >= max_age_sec."""
    hung = []
    for raw_line in (ps_output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("PID "):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            etimes = int(parts[1])
        except ValueError:
            continue
        if pid <= 1 or etimes < max_age_sec:
            continue
        rest = parts[2].strip()
        # `ps -eo pid,etimes,comm,args` aligns comm in a 15-char column, or comm is the first token of rest
        comm = rest[:15].strip() if len(rest) > 15 else rest.split(None, 1)[0]
        args = rest[15:].strip() if len(rest) > 15 and rest[15:16] == " " else (rest.split(None, 1)[1] if " " in rest else rest)
        if comm in _HARNESS_COMMS or any(args.startswith(pfx) for pfx in _HARNESS_ARG_PREFIXES):
            continue
        hung.append({"pid": pid, "etimes": etimes, "comm": comm, "args": args})
    return hung


def list_container_hung_subprocesses(cname: str, max_age_sec: int = WATCHDOG_SUBPROCESS_TIMEOUT_SEC) -> list[dict]:
    try:
        res = subprocess.run(
            ["docker", "exec", cname, "ps", "-eo", "pid,etimes,comm,args"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return []
        return find_hung_subprocesses(res.stdout, max_age_sec=max_age_sec)
    except Exception as e:
        print(f"WARNING: failed to inspect processes in {cname}: {e}", file=sys.stderr)
        return []


def kill_container_subprocess(cname: str, child_pid: int) -> bool:
    try:
        res = subprocess.run(
            ["docker", "exec", cname, "kill", "-9", str(child_pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return res.returncode == 0
    except Exception as e:
        print(f"WARNING: failed to kill PID {child_pid} in {cname}: {e}", file=sys.stderr)
        return False


def default_wake_roles(env=None):
    if env is None:
        env = os.environ
    raw = env.get("WATCHDOG_WAKE_ROLES", "")
    roles = {r.strip() for r in raw.split(",") if r.strip()}
    return roles or {"lead", "eval-lead"}


WATCHDOG_WAKE_ROLES = default_wake_roles(os.environ)


def should_wake(agent_slug: str, template: str, wake_roles: set[str] | None = None) -> bool:
    if wake_roles is None:
        wake_roles = default_wake_roles()
    role = (template or "").strip()
    if role:
        return role in wake_roles
    return any(agent_slug == r or agent_slug.endswith(f"-{r}") for r in wake_roles)


def mint_admin_jwt():
    if not os.path.exists(HUB_DB):
        sys.exit(f"hub.db not found at {HUB_DB}")
    if not os.path.exists(HUB_ENV):
        sys.exit(f"hub.env not found at {HUB_ENV}")

    conn = sqlite3.connect(f"file:{HUB_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    user = None
    if ADMIN_EMAIL:
        user = cursor.execute("SELECT id, email, role FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
    if not user:
        user = cursor.execute("SELECT id, email, role FROM users WHERE role = 'admin' ORDER BY created ASC LIMIT 1").fetchone()
    if not user:
        user = cursor.execute("SELECT id, email, role FROM users ORDER BY created ASC LIMIT 1").fetchone()
    if not user:
        sys.exit(f"no user found in {HUB_DB}")

    secret = None
    with open(HUB_ENV, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("SCION_SERVER_SESSION_SECRET="):
                secret = line.split("=", 1)[1].strip()
    if not secret:
        sys.exit(f"SCION_SERVER_SESSION_SECRET not found in {HUB_ENV}")

    key = hashlib.sha256(("scion-hub-signing-key:user_signing_key:" + secret).encode()).digest()

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    claims = {
        "iss": "scion-hub",
        "sub": user["id"],
        "aud": ["scion-hub-api"],
        "exp": now + 300,
        "nbf": now - 10,
        "iat": now,
        "uid": user["id"],
        "email": user["email"],
        "name": user["email"],
        "role": user["role"],
        "type": "access",
        "client": "web",
    }
    signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + b64(json.dumps(claims).encode())
    sig = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64(sig)


def api(method, path, body=None):
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = mint_admin_jwt()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        HUB_LOCAL_URL + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        err_msg = err.read().decode()[:500]
        return err.code, {"error": err_msg}
    except Exception as err:
        return 500, {"error": str(err)}


def log_alert(event_type, payload, severity="ERROR"):
    print(f"[{severity}] {event_type}: {json.dumps(payload)}")
    if not HUB_PROJECT_ID or not shutil.which("gcloud"):
        return
    cmd = [
        "gcloud",
        "logging",
        "write",
        "scion-factory-alerts",
        json.dumps(payload),
        "--payload-type=json",
        f"--severity={severity}",
        f"--project={HUB_PROJECT_ID}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            print(f"WARNING: gcloud logging write failed (code {res.returncode}): {res.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: failed to emit log alert: {e}", file=sys.stderr)


def load_state():
    state = {"restarts": {}, "alerted": {}, "last_user_msg_ts": None}
    if os.path.exists(WATCHDOG_STATE):
        try:
            with open(WATCHDOG_STATE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    state.update(loaded)
        except Exception as e:
            print(f"WARNING: error reading {WATCHDOG_STATE}: {e}", file=sys.stderr)

    # Prune restarts older than 3600s
    now = time.time()
    pruned_restarts = {}
    for k, timestamps in state.get("restarts", {}).items():
        recent = [ts for ts in timestamps if now - ts < 3600]
        if recent:
            pruned_restarts[k] = recent
    state["restarts"] = pruned_restarts
    return state


def save_state(state):
    tmp_dir = os.path.dirname(WATCHDOG_STATE) or "."
    fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix=".watchdog-state.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, WATCHDOG_STATE)


OLD_DISABLED_HOOK = '"| SCION_TELEMETRY_ENABLED=false sciontool hook --dialect=antigravity"'
CLEAN_HOOK = '"| sciontool hook --dialect=antigravity"'


def patch_provisioner_file(path):
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if OLD_DISABLED_HOOK in src:
        bak = f"{path}.bak-{datetime.date.today():%Y%m%d}"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        new_src = src.replace(OLD_DISABLED_HOOK, CLEAN_HOOK)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_src)
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
        return True
    return False


# ---------------------------------------------------------------------------
# Check 1: Antigravity provisioner drift guard
# ---------------------------------------------------------------------------
def check_antigravity_drift(apply):
    pattern = os.path.join(SCION_HOME, "workspace-storage", "hubs", "*", "harness-configs", "global", "antigravity", "provision.py")
    files = glob.glob(pattern)
    drifted = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if "Gemini 4 Argon (High)" not in content or "SCION_TELEMETRY_ENABLED=false" in content:
                drifted.append(path)
        except Exception as e:
            print(f"WARNING: could not read {path}: {e}", file=sys.stderr)

    if not drifted:
        print("Check 1 (Antigravity provisioner): OK (no drift, Argon and clean telemetry hooks active)")
        return True

    print(f"Check 1 (Antigravity provisioner): DRIFT DETECTED in {len(drifted)} file(s)")
    for p in drifted:
        print(f"  unpatched: {p}")

    if not apply:
        print("  (dry-run: run with --apply to re-patch)")
        return False

    patched_count = 0
    for p in drifted:
        if patch_provisioner_file(p):
            patched_count += 1

    if os.path.exists(PATCH_SCRIPT):
        subprocess.run([sys.executable, PATCH_SCRIPT, "--apply"], capture_output=True, text=True)

    print(f"  Successfully patched {patched_count} drifted provisioner(s).")
    log_alert("WATCHDOG_RECOVERY", {"event": "ANTIGRAVITY_DRIFT_PATCHED", "files": drifted}, severity="INFO")
    return True


# ---------------------------------------------------------------------------
# Check 2: Heartbeat schedule guard
# ---------------------------------------------------------------------------
def check_heartbeat_schedules(conn, apply):
    if not FACTORY_PROJECTS:
        print("Check 2 (Heartbeat guard): OK (0 factory projects configured)")
        return True
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in FACTORY_PROJECTS)
    cursor.execute(
        f"""
        SELECT s.id, s.project_id, p.slug as project_slug, s.name, s.cron_expr, s.status, s.last_run_at, s.last_run_status, s.run_count, s.error_count
        FROM schedules s
        JOIN projects p ON s.project_id = p.id
        WHERE p.slug IN ({placeholders})
          AND s.status <> 'deleted'
          AND (s.name LIKE '%heartbeat%' OR s.name LIKE 'eval-sweep%')
        ORDER BY p.slug, s.name
        """,
        FACTORY_PROJECTS,
    )
    rows = cursor.fetchall()
    all_ok = True
    for s in rows:
        sched_id = s["id"]
        proj_slug = s["project_slug"]
        sched_name = s["name"]
        status = s["status"]
        if status != "active":
            all_ok = False
            print(f"Check 2 (Heartbeat guard): Schedule '{sched_name}' in project '{proj_slug}' is {status} (expected 'active')")
            if apply:
                # Resume via Hub API
                http_status, out = api("POST", f"/api/v1/projects/{s['project_id']}/schedules/{sched_id}/resume")
                if http_status in (200, 204):
                    print(f"  Resumed schedule '{sched_name}' via API.")
                    log_alert(
                        "WATCHDOG_RECOVERY",
                        {"event": "SCHEDULE_RESUMED", "schedule": sched_name, "project": proj_slug},
                        severity="WARNING",
                    )
                else:
                    # Fallback to direct SQLite update if API refused
                    print(f"  API resume failed ({http_status}: {out.get('error')}); restoring via database...")
                    try:
                        wconn = sqlite3.connect(HUB_DB)
                        wconn.execute("UPDATE schedules SET status = 'active', updated = CURRENT_TIMESTAMP WHERE id = ?", (sched_id,))
                        wconn.commit()
                        wconn.close()
                        print(f"  Database updated: schedule '{sched_name}' restored to active.")
                        log_alert(
                            "WATCHDOG_RECOVERY",
                            {"event": "SCHEDULE_RESTORED_DB", "schedule": sched_name, "project": proj_slug},
                            severity="WARNING",
                        )
                    except Exception as e:
                        print(f"  ERROR restoring schedule in DB: {e}", file=sys.stderr)
            else:
                print("  (dry-run: would resume schedule)")

    if all_ok:
        print(f"Check 2 (Heartbeat guard): OK ({len(rows)} heartbeat schedules active)")
    return all_ok


# ---------------------------------------------------------------------------
# Check 3: Agent liveness & circuit-breaker recovery
# ---------------------------------------------------------------------------
def get_docker_containers():
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return {}
        containers = {}
        for line in res.stdout.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) == 2:
                containers[parts[0].strip()] = parts[1].strip()
        return containers
    except Exception as e:
        print(f"WARNING: failed to list docker containers: {e}", file=sys.stderr)
        return {}


def lead_agent_for(project_slug, agent_slugs):
    """The lead of a factory project, by naming convention (docs/plans/2026-10-22-monorepo-factories.md).

    Monorepo factories prefix agent names with the build slug: `<slug>-lead` in the build project and
    `<slug>-eval-lead` in `<slug>-eval`. Single-factory setups use bare `lead` / `eval-lead`.
    """
    if project_slug.endswith("-eval"):
        candidates = [f"{project_slug[:-len('-eval')]}-eval-lead", "eval-lead"]
    else:
        candidates = [f"{project_slug}-lead", "lead"]
    present = set(agent_slugs)
    return next((c for c in candidates if c in present), "lead")


def check_agent_liveness(conn, state, apply):
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in FACTORY_PROJECTS)
    cursor.execute(f"""
        SELECT a.id, a.project_id, p.slug as project_slug, a.slug as agent_slug, a.name, a.phase, a.status, a.template
        FROM agents a
        JOIN projects p ON a.project_id = p.id
        WHERE p.slug IN ({placeholders}) AND a.deleted_at IS NULL
        ORDER BY p.slug, a.slug
    """, FACTORY_PROJECTS)
    agents = cursor.fetchall()
    docker_containers = get_docker_containers()

    now = time.time()
    restarts_map = state.setdefault("restarts", {})
    alerted_map = state.setdefault("alerted", {})

    agents_by_project = {}
    for a in agents:
        agents_by_project.setdefault(a["project_slug"], []).append(a["agent_slug"])

    for a in agents:
        proj_slug = a["project_slug"]
        agent_slug = a["agent_slug"]
        pid = a["project_id"]
        phase = a["phase"] or ""
        key = f"{proj_slug}/{agent_slug}"
        cname = f"{proj_slug}--{agent_slug}"
        cstatus = docker_containers.get(cname, "")
        is_container_running = cstatus.startswith("Up ")

        recent_restarts = restarts_map.get(key, [])
        circuit_broken = len(recent_restarts) >= MAX_RESTARTS_PER_HOUR

        # Issue 1: Suspended agent
        if phase == "suspended":
            print(f"Check 3 (Agent liveness): Agent '{key}' is SUSPENDED")
            template = a["template"] or ""
            wake_roles = default_wake_roles(os.environ) if "WATCHDOG_WAKE_ROLES" in os.environ else WATCHDOG_WAKE_ROLES
            if not should_wake(agent_slug, template, wake_roles):
                print("  left asleep (lead wakes it with a brief)")
                continue
            if circuit_broken:
                print(f"  CIRCUIT BREAKER: {key} reached {len(recent_restarts)} restarts/hr (limit: {MAX_RESTARTS_PER_HOUR}). Skipping restart.")
                if now - alerted_map.get(key, 0) > 3600:
                    log_alert(
                        "WATCHDOG_CIRCUIT_BREAKER",
                        {"agent": agent_slug, "project": proj_slug, "phase": phase, "restarts_in_hour": len(recent_restarts)},
                        severity="ERROR",
                    )
                    alerted_map[key] = now
            else:
                if apply:
                    wake_msg = (
                        "Watchdog wake: resume your current task or check scion list / gh issue list, and end your turn with sciontool status."
                    )
                    http_status, out = api("POST", f"/api/v1/projects/{pid}/agents/{agent_slug}/message", {"message": wake_msg, "wake": True})
                    if http_status in (200, 204):
                        print(f"  Successfully woke suspended agent '{key}'.")
                        restarts_map.setdefault(key, []).append(now)
                        log_alert("WATCHDOG_RECOVERY", {"event": "AGENT_WAKE", "agent": agent_slug, "project": proj_slug}, severity="INFO")
                    else:
                        print(f"  ERROR: Failed to wake agent '{key}' ({http_status}: {out.get('error')})", file=sys.stderr)
                else:
                    print("  (dry-run: would wake agent via API)")

        # Issue 2: Stopped / Error phase OR container exited while phase == running
        elif phase in ("stopped", "error") or (phase == "running" and not is_container_running and cstatus):
            reason = f"phase={phase}" if phase in ("stopped", "error") else f"container exited ({cstatus})"
            print(f"Check 3 (Agent liveness): Agent '{key}' is UNHEALTHY ({reason})")

            if circuit_broken:
                print(f"  CIRCUIT BREAKER: {key} reached {len(recent_restarts)} restarts/hr (limit: {MAX_RESTARTS_PER_HOUR}). Skipping restart.")
                if now - alerted_map.get(key, 0) > 3600:
                    log_alert(
                        "WATCHDOG_CIRCUIT_BREAKER",
                        {"agent": agent_slug, "project": proj_slug, "reason": reason, "restarts_in_hour": len(recent_restarts)},
                        severity="ERROR",
                    )
                    alerted_map[key] = now
            else:
                if apply:
                    restarted = False
                    # Attempt Hub API start
                    force = (phase == "error")
                    http_status, out = api("POST", f"/api/v1/projects/{pid}/agents/{agent_slug}/start", {"forceResume": force})
                    if http_status in (200, 204):
                        print(f"  Started agent '{key}' via Hub API.")
                        restarted = True
                    elif cname in docker_containers:
                        # Fallback to docker start if container exists
                        print(f"  API start returned {http_status}; falling back to docker start {cname}...")
                        dres = subprocess.run(["docker", "start", cname], capture_output=True, text=True)
                        if dres.returncode == 0:
                            print(f"  Successfully started docker container '{cname}'.")
                            restarted = True
                        else:
                            print(f"  ERROR: docker start {cname} failed: {dres.stderr.strip()}", file=sys.stderr)

                    if restarted:
                        restarts_map.setdefault(key, []).append(now)
                        lead = lead_agent_for(proj_slug, agents_by_project.get(proj_slug, []))
                        if agent_slug != lead:
                            api("POST", f"/api/v1/projects/{pid}/agents/{lead}/message", {
                                "message": f"PO -> {lead}: watchdog auto-restarted agent {agent_slug} (was {reason}). Please check its status and re-send its active task brief if needed."
                            })
                        log_alert(
                            "WATCHDOG_RECOVERY",
                            {"event": "AGENT_RESTART", "agent": agent_slug, "project": proj_slug, "reason": reason},
                            severity="WARNING",
                        )
                else:
                    print("  (dry-run: would restart agent and notify lead)")

        # Issue 3: Running container with a hung non-harness child subprocess (e.g. un-timed server or deadlocked socket)
        elif phase == "running" and is_container_running:
            hung = list_container_hung_subprocesses(cname, WATCHDOG_SUBPROCESS_TIMEOUT_SEC)
            for h in hung:
                child_pid = h["pid"]
                etimes = h["etimes"]
                cmd_preview = (h["args"] or h["comm"])[:120]
                print(f"Check 3 (Agent liveness): Agent '{key}' has HUNG SUBPROCESS pid={child_pid} ({etimes}s): {cmd_preview}")
                if apply:
                    if kill_container_subprocess(cname, child_pid):
                        print(f"  Killed hung subprocess pid={child_pid} in '{cname}'.")
                        api("POST", f"/api/v1/projects/{pid}/agents/{agent_slug}/message", {
                            "message": (
                                f"Watchdog -> {agent_slug}: killed hung subprocess PID {child_pid} ({cmd_preview}) after {etimes}s. "
                                "Prefer in-process test clients (TestClient / test_client()) or wrap live commands in timeout 30s, then continue your brief."
                            )
                        })
                        lead = lead_agent_for(proj_slug, agents_by_project.get(proj_slug, []))
                        if agent_slug != lead:
                            api("POST", f"/api/v1/projects/{pid}/agents/{lead}/message", {
                                "message": (
                                    f"Watchdog -> {lead}: reaped hung subprocess PID {child_pid} ({etimes}s: {cmd_preview}) in {agent_slug} "
                                    "and nudged the worker to continue its brief."
                                )
                            })
                        log_alert(
                            "WATCHDOG_RECOVERY",
                            {
                                "event": "HUNG_SUBPROCESS_KILLED",
                                "agent": agent_slug,
                                "project": proj_slug,
                                "pid": child_pid,
                                "elapsed_sec": etimes,
                                "command": cmd_preview,
                            },
                            severity="WARNING",
                        )
                else:
                    print(f"  (dry-run: would kill pid={child_pid} in {cname} and notify {agent_slug} + lead)")


# ---------------------------------------------------------------------------
# Check 4: PO Inbox Escalation Forwarder
# ---------------------------------------------------------------------------
def check_po_inbox(conn, state, apply):
    cursor = conn.cursor()
    admin_user = None
    if ADMIN_EMAIL:
        admin_user = cursor.execute("SELECT id, email FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
    if not admin_user:
        admin_user = cursor.execute("SELECT id, email FROM users WHERE role = 'admin' ORDER BY created ASC LIMIT 1").fetchone()

    admin_user_id = admin_user["id"] if admin_user else None
    admin_email_val = admin_user["email"] if admin_user else ADMIN_EMAIL

    recipients = []
    if admin_email_val:
        recipients.append(f"user:{admin_email_val}")
    if admin_user_id:
        recipients.append(f"user:{admin_user_id}")

    if not recipients and not admin_user_id:
        print("Check 4 (PO Inbox): skipped (no admin user/email identified)")
        return

    placeholders = ",".join("?" for _ in recipients)
    params = list(recipients)

    last_ts = state.get("last_user_msg_ts")

    if last_ts is None:
        # Initialize bookmark to the latest existing message
        sql = f"SELECT MAX(created) as max_c FROM messages WHERE (recipient IN ({placeholders}) OR recipient_id = ?)"
        row = cursor.execute(sql, params + [admin_user_id or ""]).fetchone()
        max_c = row["max_c"] if row and row["max_c"] else datetime.datetime.now(datetime.timezone.utc).isoformat()
        state["last_user_msg_ts"] = str(max_c)
        print(f"Check 4 (PO Inbox): initialized bookmark at {max_c}")
        return

    sql = f"""
        SELECT id, project_id, sender, sender_id, recipient, recipient_id, msg, type, created
        FROM messages
        WHERE (recipient IN ({placeholders}) OR recipient_id = ?)
          AND created > ?
        ORDER BY created ASC
    """
    rows = cursor.execute(sql, params + [admin_user_id or "", last_ts]).fetchall()

    if not rows:
        print("Check 4 (PO Inbox): OK (no new messages to PO)")
        return

    # Scion automatically sends `type = 'state-change'` notifications (COMPLETED / STALLED) to the parent user who
    # started lead and eval-lead. Only explicit `instruction` messages represent human-on-the-loop PO escalations.
    escalations = [r for r in rows if (r["type"] or "instruction") != "state-change"]
    if apply:
        state["last_user_msg_ts"] = str(rows[-1]["created"])

    if not escalations:
        print(f"Check 4 (PO Inbox): OK ({len(rows)} state-change notification(s) ignored; no PO escalations)")
        return

    print(f"Check 4 (PO Inbox): {len(escalations)} new escalation(s) to PO")
    for r in escalations:
        created_str = str(r["created"])
        msg_text = r["msg"] or ""
        sender = r["sender"] or ""
        msg_type = r["type"] or "instruction"
        summary = msg_text.strip().replace("\n", " ")[:150]

        print(f"  PO message from {sender} at {created_str}: {summary}")
        if apply:
            payload = {
                "event": "PO_ESCALATION",
                "message_id": r["id"],
                "project_id": r["project_id"],
                "sender": sender,
                "recipient": r["recipient"],
                "type": msg_type,
                "created": created_str,
                "summary": summary,
                "message": msg_text[:2000],
            }
            log_alert("PO_ESCALATION", payload, severity="ERROR")
        else:
            print("    (dry-run: would emit Cloud Logging PO_ESCALATION alert)")


def knowledge_plane_sources(projects, sources_env, legacy_source, has_knowledge_plane=None):
    """Where each project's shared knowledge-plane copy comes from: {project: (source project, path in its checkout)}.

    KNOWLEDGE_PLANE_SOURCES (several factories, e.g. one per monorepo product) maps a factory's build slug to the
    knowledge plane inside that project's shared checkout: "project1=src/project1/knowledge-plane,project2=...".
    `<slug>` and `<slug>-eval` both get it. Any other project whose factory checkout has `knowledge-plane/`
    (`has_knowledge_plane(<slug>)`, where `<slug>-eval` follows `<slug>`) gets its own, so compiled kits and monorepo
    factories can share one Hub (L28). The rest get `knowledge-plane/` of KNOWLEDGE_PLANE_SOURCE_PROJECT (without the
    map it defaults to the first project; with the map it must be set explicitly), or are skipped.
    """
    mapping = {}
    for entry in [e.strip() for e in (sources_env or "").split(",") if e.strip()]:
        factory, sep, path = entry.partition("=")
        factory, path = factory.strip(), path.strip().strip("/")
        parts = path.split("/")
        if not sep or not factory or not path or entry.split("=", 1)[1].strip().startswith("/") or ".." in parts:
            raise ValueError(f"KNOWLEDGE_PLANE_SOURCES entry {entry!r}: use <build slug>=<relative path>")
        mapping[factory] = path
    # With a map, the single source applies only when KNOWLEDGE_PLANE_SOURCE_PROJECT is set explicitly.
    source = legacy_source if mapping else (legacy_source or (projects[0] if projects else ""))
    sources = {}
    for p in projects:
        mapped = p[:-len("-eval")] if p.endswith("-eval") and p not in mapping else p
        factory = p[:-len("-eval")] if p.endswith("-eval") else p
        if mapped in mapping:
            sources[p] = (mapped, mapping[mapped])
        elif has_knowledge_plane and has_knowledge_plane(factory):
            sources[p] = (factory, "knowledge-plane")
        elif source:
            sources[p] = (source, "knowledge-plane")
    return sources


def check_shared_knowledge_plane(conn, apply=False):
    """Keep each project's shared directory `knowledge-plane` in sync with its factory's knowledge plane.

    Agents read the copy at /scion-volumes/knowledge-plane. The shared directory must exist (scion-admin.py
    shared-dirs) and the source must be a shared checkout (~/.scion/projects/<slug>/). See knowledge_plane_sources().
    """
    try:
        sources = knowledge_plane_sources(
            FACTORY_PROJECTS, os.environ.get("KNOWLEDGE_PLANE_SOURCES", ""),
            os.environ.get("KNOWLEDGE_PLANE_SOURCE_PROJECT", ""),
            lambda slug: os.path.isdir(os.path.join(SCION_HOME, "projects", slug, "knowledge-plane")))
    except ValueError as e:
        print(f"Check 5 (Shared Knowledge Plane): ERROR {e}", file=sys.stderr)
        return
    if not sources:
        return

    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in FACTORY_PROJECTS)
    rows = cursor.execute(
        f"SELECT id, slug FROM projects WHERE slug IN ({placeholders})",
        FACTORY_PROJECTS,
    ).fetchall()

    for r in rows:
        slug = r["slug"]
        if slug not in sources:
            print(f"Check 5 (Shared Knowledge Plane): {slug}: skipped (no knowledge plane source)")
            continue
        source_project, rel = sources[slug]
        src_dir = os.path.join(SCION_HOME, "projects", source_project, rel)
        id8 = r["id"].replace("-", "")[:8]  # config.SharedDirHostPath: <slug>__<first 8 hex of the project ID>
        dst_dir = os.path.join(SCION_HOME, "project-configs", f"{slug}__{id8}", "shared-dirs", "knowledge-plane")
        if not os.path.isdir(src_dir):
            print(f"Check 5 (Shared Knowledge Plane): {slug}: source missing ({src_dir}; needs a shared checkout)")
            continue
        # The Hub creates project-configs/<slug>__<id8>/ with the project; shared-dirs/ appears only when the first
        # agent starts. Fill it before that, so the first agent already finds the knowledge plane (L28).
        if not os.path.isdir(os.path.dirname(os.path.dirname(dst_dir))):
            print(f"Check 5 (Shared Knowledge Plane): {slug}: skipped (project not set up on this broker)")
            continue
        if apply:
            os.makedirs(dst_dir, exist_ok=True)
            res = subprocess.run(
                ["rsync", "-ai", "--delete", src_dir + "/", dst_dir + "/"],
                capture_output=True,
                text=True,
                check=False,
            )
            changed = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
            if res.returncode != 0:
                print(
                    f"Check 5 (Shared Knowledge Plane): {slug}: ERROR rsync exited {res.returncode}: {(res.stderr or '').strip()}",
                    file=sys.stderr,
                )
            elif changed:
                print(f"Check 5 (Shared Knowledge Plane): {slug}: synced ({len(changed)} changes) from {src_dir}")
            else:
                print(f"Check 5 (Shared Knowledge Plane): {slug}: up to date with {src_dir}")
        else:
            print(f"Check 5 (Shared Knowledge Plane): {slug}: would sync from {src_dir}")


# ---------------------------------------------------------------------------
# Installation helper
# ---------------------------------------------------------------------------
def watchdog_env_content(path):
    """watchdog.env for the cron runs: every setting the checks read, so cron behaves like the install run."""
    lines = ["# Factory watchdog configuration",
             f"ADMIN_EMAIL={ADMIN_EMAIL}",
             f"HUB_PROJECT_ID={HUB_PROJECT_ID}",
             f"FACTORY_PROJECTS={','.join(FACTORY_PROJECTS)}",
             f"MAX_RESTARTS_PER_HOUR={MAX_RESTARTS_PER_HOUR}"]
    for key in (
        "KNOWLEDGE_PLANE_SOURCES",
        "KNOWLEDGE_PLANE_SOURCE_PROJECT",
        "WATCHDOG_WAKE_ROLES",
        "WATCHDOG_SUBPROCESS_TIMEOUT_SEC",
    ):
        if os.environ.get(key):
            lines.append(f"{key}={os.environ[key]}")
    lines.append(f"PATH={path}")
    return "\n".join(lines) + "\n"


def install_cron():
    os.makedirs(SCION_HOME, exist_ok=True)
    dst_script = os.path.join(SCION_HOME, "factory-watchdog.py")
    src_script = os.path.abspath(__file__)
    if os.path.realpath(src_script) != os.path.realpath(dst_script):
        shutil.copy2(src_script, dst_script)
        os.chmod(dst_script, 0o700)
        print(f"Copied script to {dst_script} (mode 0700)")

    # Also copy patch-antigravity-hooks.py if nearby
    src_patch = os.path.join(os.path.dirname(src_script), "patch-antigravity-hooks.py")
    if os.path.exists(src_patch) and not os.path.exists(PATCH_SCRIPT):
        shutil.copy2(src_patch, PATCH_SCRIPT)
        os.chmod(PATCH_SCRIPT, 0o700)
        print(f"Copied patch script to {PATCH_SCRIPT} (mode 0700)")

    # Write watchdog.env
    cur_path = os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    if "/snap/bin" not in cur_path.split(":"):
        cur_path = f"/snap/bin:{cur_path}"

    env_content = watchdog_env_content(cur_path)
    with open(WATCHDOG_ENV, "w", encoding="utf-8") as f:
        f.write(env_content)
    os.chmod(WATCHDOG_ENV, 0o600)
    print(f"Wrote {WATCHDOG_ENV} (mode 0600)")

    # Update crontab
    cron_line = f"*/5 * * * * /usr/bin/python3 {dst_script} --apply >> {os.path.join(SCION_HOME, 'watchdog.log')} 2>&1"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [line for line in cur.stdout.splitlines() if "factory-watchdog.py" not in line]
    lines.append(cron_line)
    new_crontab = "\n".join(lines).strip() + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    print("Registered watchdog job in crontab:")
    print(f"  {cron_line}")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def _is_project_lead_sleeping(store, project_slug: str) -> bool:
    if store is None or not project_slug or not hasattr(store, "get_document"):
        return False
    try:
        doc = store.get_document("factory_control_state", f"delta__{project_slug}")
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    if "lead_sleeping" in doc:
        return bool(doc.get("lead_sleeping"))
    if isinstance(doc.get("state_json"), str):
        try:
            return bool(json.loads(doc["state_json"]).get("lead_sleeping"))
        except Exception:
            return False
    return False


def run_api_watchdog(client, state: dict, apply: bool = False, runtime: str = "kubernetes", store=None) -> None:
    alerted = state.get("alerted")
    if isinstance(alerted, list):
        alerted = {k: 0 for k in alerted}
    elif not isinstance(alerted, dict):
        alerted = {}
    state["alerted"] = alerted

    projects = client.list_projects()
    factory_projects = os.environ.get("FACTORY_PROJECTS")
    if factory_projects:
        factory_projects = [p.strip() for p in factory_projects.split(",") if p.strip()]
        projects = [p for p in projects if p["slug"] in factory_projects]

    # Check 2: Schedule Guard
    for p in projects:
        pid = p.get("id") or p["slug"]
        try:
            sched_resp = client.request("GET", f"/api/v1/projects/{pid}/schedules")[1]
            schedules = sched_resp.get("schedules", []) if isinstance(sched_resp, dict) else (sched_resp or [])
            for s in schedules:
                if s.get("status") in ("paused", "error"):
                    if apply:
                        if s.get("status") == "paused":
                            client.request("POST", f"/api/v1/projects/{pid}/schedules/{s['id']}/resume")
                        else:
                            client.request("PATCH", f"/api/v1/projects/{pid}/schedules/{s['id']}", body={"status": "active"})
        except Exception:
            pass

    # Check 3: Agent health + hung subprocess reaping
    for p in projects:
        pid = p.get("id") or p["slug"]
        proj_slug = p.get("slug") or pid
        lead_sleeping = _is_project_lead_sleeping(store, proj_slug)
        agents = client.list_agents(pid)
        for a in agents:
            slug = a.get("slug") or a.get("name")
            if not slug: continue
            
            phase = a.get("phase") or a.get("status")
            if phase == "running":
                try:
                    res = client.exec_agent(pid, slug, ["ps", "-eo", "pid,etimes,comm,args"], timeout=10)
                    if res:
                        hung_max = int(os.environ.get("HUNG_PROC_MAX_SEC", str(WATCHDOG_SUBPROCESS_TIMEOUT_SEC)))
                        for h in find_hung_subprocesses(res[1], max_age_sec=hung_max):
                            if apply:
                                client.exec_agent(pid, slug, ["kill", "-9", str(h["pid"])], timeout=5)
                except Exception:
                    pass
            elif phase in ("error", "halted", "stopped"):
                if lead_sleeping:
                    continue
                if phase == "stopped" and not should_wake(slug, a.get("template", "")):
                    continue
                    
                key = f"{pid}/{slug}"
                restarts = state["restarts"].get(key, [])
                now = time.time()
                restarts = [ts for ts in restarts if now - ts < 3600]
                
                max_restarts = int(os.environ.get("MAX_RESTARTS_PER_HOUR", "3"))
                if len(restarts) < max_restarts:
                    if apply:
                        restarts.append(now)
                        state["restarts"][key] = restarts
                        client.ensure_agent_started(pid, slug, force_resume=True)
                else:
                    if apply and key not in state["alerted"]:
                        state["alerted"][key] = time.time()
                        
    # Check 4: PO Inbox escalations
    try:
        msgs_resp = client.request("GET", "/api/v1/messages?limit=100")[1]
        msgs = (
            msgs_resp.get("messages")
            if isinstance(msgs_resp, dict) and isinstance(msgs_resp.get("messages"), list)
            else (msgs_resp.get("items", []) if isinstance(msgs_resp, dict) else (msgs_resp or []))
        )

        def _msg_ts(m: dict) -> str:
            return str(m.get("createdAt") or m.get("created") or "")

        last_ts = state.get("last_user_msg_ts", "")
        new_msgs = [
            m
            for m in msgs
            if isinstance(m, dict) and m.get("type") != "state-change" and _msg_ts(m) > last_ts
        ]
        if new_msgs and apply:
            if last_ts:
                for m in new_msgs:
                    msg_text = str(m.get("msg") or m.get("message") or "")
                    log_alert(
                        "PO_ESCALATION",
                        {
                            "event": "PO_ESCALATION",
                            "message_id": m.get("id", ""),
                            "project_id": m.get("projectId") or m.get("project_id", ""),
                            "sender": m.get("sender", ""),
                            "recipient": m.get("recipient", ""),
                            "type": m.get("type") or "instruction",
                            "created": _msg_ts(m),
                            "summary": msg_text.strip().replace("\n", " ")[:150],
                            "message": msg_text[:2000],
                        },
                        severity="ERROR",
                    )
            state["last_user_msg_ts"] = max(_msg_ts(m) for m in new_msgs)
    except Exception:
        pass
        
    # Check 5: Shared knowledge-plane sync
    kp_dir = os.environ.get("KNOWLEDGE_PLANE_LOCAL_DIR")
    if kp_dir and os.path.isdir(kp_dir):
        for p in projects:
            try:
                client.sync_shared_dir(p.get("id") or p["slug"], "knowledge-plane", kp_dir, apply=apply)
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser(description="Scion Factory Watchdog")
    parser.add_argument("--apply", action="store_true", help="Execute recovery actions and advance bookmarks")
    parser.add_argument("--install-cron", action="store_true", help="Install script and schedule every 5 min in crontab")
    parser.add_argument("--json", action="store_true", help="Output status in JSON format")
    args = parser.parse_args()


    if args.install_cron:
        install_cron()
        if not args.apply:
            return

    hub_url = os.environ.get("HUB_URL", "").strip()
    if hub_url:
        import hub_client
        import factory_store
        
        client = hub_client.HubClient.from_env()
        backend = os.environ.get("WATCHDOG_STATE_BACKEND", "")
        if not backend and os.environ.get("FIRESTORE_PROJECT") and not os.environ.get("WATCHDOG_STATE"):
            backend = "firestore"
            
        store = None
        state_id = "watchdog__default"
        if backend == "firestore":
            store = factory_store.store_from_env(dry_run=not args.apply)
            state = factory_store.load_control_state(store, state_id)
        else:
            state_file = os.environ.get("WATCHDOG_STATE", "/tmp/watchdog.json")
            if os.path.exists(state_file):
                import json
                with open(state_file, "r") as f:
                    state = json.load(f)
            else:
                state = {"restarts": {}, "alerted": [], "last_user_msg_ts": ""}
                
        run_api_watchdog(client, state, apply=args.apply, runtime=os.environ.get("AGENT_RUNTIME", "kubernetes"), store=store)
        
        if args.apply:
            if backend == "firestore":
                factory_store.save_control_state(store, state_id, state)
            else:
                import json
                with open(state_file, "w") as f:
                    json.dump(state, f)
        return 0

    if not os.path.exists(HUB_DB):
        sys.exit(f"hub.db not found at {HUB_DB}")
    conn = sqlite3.connect(f"file:{HUB_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    state = load_state()

    print(f"=== Scion Factory Watchdog (apply={args.apply}) ===")
    check_antigravity_drift(args.apply)
    check_heartbeat_schedules(conn, args.apply)
    check_agent_liveness(conn, state, args.apply)
    check_po_inbox(conn, state, args.apply)
    check_shared_knowledge_plane(conn, args.apply)

    if args.apply:
        save_state(state)

    print("=== Watchdog run completed ===")


if __name__ == "__main__":
    main()
