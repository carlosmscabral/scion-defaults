"""Shared directories (scratchpad, knowledge-plane) per factory project, and a knowledge plane per monorepo factory.

Spec: docs/plans/2026-10-22-monorepo-factories.md, "Part 2: shared directories".
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


admin = _load("scion_admin", ROOT / "scripts" / "scion-admin.py")


# --- scion-admin.py project-create -------------------------------------------------------------------------------

def test_project_body_for_a_shared_git_project():
    """The Hub takes the branch from the scion.dev/default-branch label; CreateProjectRequest has no branch field
    (pkg/hub/handlers_projects_core.go), so a top-level branch key is silently ignored and the clone gets `main`."""
    body = admin.project_create_body("project1-eval", "https://github.com/o/r.git", "eval-dashboard", "shared", "tok")
    assert body == {"name": "project1-eval", "slug": "project1-eval", "gitRemote": "https://github.com/o/r.git",
                    "workspaceMode": "shared", "githubToken": "tok",
                    "labels": {"scion.dev/default-branch": "eval-dashboard",
                               "scion.dev/clone-url": "https://github.com/o/r.git",
                               "scion.dev/source-url": "https://github.com/o/r.git"}}


def test_project_body_requires_an_https_git_url():
    with pytest.raises(ValueError, match="https"):
        admin.project_create_body("p", "git@github.com:o/r.git", "main", "per-agent", "")


def test_project_body_without_a_token_leaves_it_out():
    body = admin.project_create_body("project1-eval", "https://github.com/o/r.git", "eval-dashboard", "per-agent", "")
    assert "githubToken" not in body
    assert body["workspaceMode"] == "per-agent"


@pytest.mark.parametrize("mode", ["shared-plain", "clone", ""])
def test_project_body_rejects_unknown_modes(mode):
    with pytest.raises(ValueError, match="workspace mode"):
        admin.project_create_body("p", "https://github.com/o/r.git", "main", mode, "")


def test_project_body_requires_a_token_for_shared_mode():
    """The Hub clones a shared project at creation; without a token a private repo fails with clone_failed."""
    with pytest.raises(ValueError, match="token"):
        admin.project_create_body("p", "https://github.com/o/r.git", "main", "shared", "")


def test_read_token_file(tmp_path):
    f = tmp_path / "tok"
    f.write_text("test-token-value\n")
    assert admin.read_token_file(str(f)) == "test-token-value"
    assert admin.read_token_file("") == ""


# --- scion-admin.py shared-dirs ----------------------------------------------------------------------------------

def test_parse_dir_specs_reads_modes():
    assert admin.parse_dir_specs("scratchpad,knowledge-plane:ro") == [
        {"name": "scratchpad", "read_only": False},
        {"name": "knowledge-plane", "read_only": True},
    ]


@pytest.mark.parametrize("spec", ["Scratch_Pad", "kp:rw:x", "kp:readonly", ""])
def test_parse_dir_specs_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        admin.parse_dir_specs(spec)


WANTED = [{"name": "scratchpad", "read_only": False}, {"name": "knowledge-plane", "read_only": True}]


def test_plan_creates_only_missing_dirs():
    create, conflicts = admin.plan_shared_dirs([{"name": "scratchpad"}], WANTED)
    assert create == [{"name": "knowledge-plane", "read_only": True}]
    assert conflicts == []


def test_plan_is_a_no_op_when_everything_exists():
    existing = [{"name": "scratchpad"}, {"name": "knowledge-plane", "read_only": True}]
    assert admin.plan_shared_dirs(existing, WANTED) == ([], [])


def test_plan_reports_a_mode_mismatch_instead_of_changing_it():
    """Changing a mode means delete + create, which deletes the contents: never automatic."""
    create, conflicts = admin.plan_shared_dirs([{"name": "knowledge-plane"}], WANTED)
    assert create == [{"name": "scratchpad", "read_only": False}]
    assert conflicts == ["knowledge-plane is read-write, wanted read-only (fix it by hand: removing it deletes its files)"]


def test_default_projects_come_from_factory_env():
    assert admin.default_projects({"FACTORY_PROJECTS": "project1, project1-eval,"}) == ["project1", "project1-eval"]
    assert admin.default_projects({"BUILD_PROJECT": "project1", "EVAL_PROJECT": "project1-eval"}) == [
        "project1", "project1-eval"]
    assert admin.default_projects({}) == []


# --- factory-watchdog.py knowledge plane per factory -------------------------------------------------------------

@pytest.fixture(scope="module")
def watchdog(tmp_path_factory):
    os.environ.setdefault("SCION_HOME", str(tmp_path_factory.mktemp("scion-home")))
    return _load("factory_watchdog_kp", ROOT / "scripts" / "factory-watchdog.py")


def test_sources_map_each_factory_and_its_eval_project(watchdog):
    sources = watchdog.knowledge_plane_sources(
        ["project1", "project1-eval", "project2", "project2-eval"],
        "project1=src/project1/knowledge-plane, project2=src/project2/knowledge-plane", "")
    assert sources == {
        "project1": ("project1", "src/project1/knowledge-plane"),
        "project1-eval": ("project1", "src/project1/knowledge-plane"),
        "project2": ("project2", "src/project2/knowledge-plane"),
        "project2-eval": ("project2", "src/project2/knowledge-plane"),
    }


def test_sources_without_a_map_keep_the_single_source(watchdog):
    assert watchdog.knowledge_plane_sources(["psearch", "psearch-eval"], "", "") == {
        "psearch": ("psearch", "knowledge-plane"), "psearch-eval": ("psearch", "knowledge-plane")}
    assert watchdog.knowledge_plane_sources(["a", "b"], "", "b") == {"a": ("b", "knowledge-plane"),
                                                                     "b": ("b", "knowledge-plane")}


def test_sources_give_each_factory_its_own_knowledge_plane_on_a_shared_hub(watchdog):
    """Full e2e: two compiled kits on one Hub, the second kit's env set KNOWLEDGE_PLANE_SOURCE_PROJECT=shop, and the
    first factory's projects would have received shop's knowledge plane. A factory whose own checkout has
    knowledge-plane/ keeps it; only projects without one fall back to the single source."""
    has_kp = {"ledger", "shop"}.__contains__
    assert watchdog.knowledge_plane_sources(["ledger", "ledger-eval", "shop", "shop-eval", "acme-eval"], "", "shop",
                                            has_kp) == {
        "ledger": ("ledger", "knowledge-plane"), "ledger-eval": ("ledger", "knowledge-plane"),
        "shop": ("shop", "knowledge-plane"), "shop-eval": ("shop", "knowledge-plane"),
        "acme-eval": ("shop", "knowledge-plane")}


def test_sources_with_a_map_skip_unmapped_projects(watchdog):
    assert watchdog.knowledge_plane_sources(["project1", "project3"], "project1=src/project1/kp", "") == {
        "project1": ("project1", "src/project1/kp")}


def test_sources_with_a_map_keep_other_factories_on_the_hub(watchdog):
    """Full e2e: a monorepo factory joined a Hub that already ran two compiled kits. Its KNOWLEDGE_PLANE_SOURCES map
    must not stop the kits' own knowledge planes from syncing."""
    has_kp = {"ledger", "shop"}.__contains__
    assert watchdog.knowledge_plane_sources(["ledger", "ledger-eval", "shop", "project1", "project1-eval"],
                                            "project1=src/project1/knowledge-plane", "", has_kp) == {
        "ledger": ("ledger", "knowledge-plane"), "ledger-eval": ("ledger", "knowledge-plane"),
        "shop": ("shop", "knowledge-plane"),
        "project1": ("project1", "src/project1/knowledge-plane"),
        "project1-eval": ("project1", "src/project1/knowledge-plane")}


@pytest.mark.parametrize("bad", ["project1", "project1=/abs/kp", "project1=../kp", "=src/kp"])
def test_sources_reject_bad_entries(watchdog, bad):
    with pytest.raises(ValueError, match="KNOWLEDGE_PLANE_SOURCES"):
        watchdog.knowledge_plane_sources(["project1"], bad, "")


def _hub(scion_home, projects):
    conn = sqlite3.connect(scion_home / "hub.db")
    conn.execute("CREATE TABLE projects (id TEXT, slug TEXT)")
    for pid, slug in projects:
        conn.execute("INSERT INTO projects VALUES (?, ?)", (pid, slug))
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.skipif(not any(os.access(os.path.join(p, "rsync"), os.X_OK)
                            for p in os.environ["PATH"].split(os.pathsep)), reason="rsync not installed")
def test_sync_copies_each_factorys_knowledge_plane_into_its_projects(watchdog, tmp_path, monkeypatch, capsys):
    home = tmp_path / ".scion"
    ids = {"project1": "11111111-aaaa", "project1-eval": "22222222-bbbb", "project2": "33333333-cccc",
           "project2-eval": "44444444-dddd"}
    conn = _hub(home.mkdir() or home, list((v, k) for k, v in ids.items()))
    for p in ("project1", "project2"):
        kp = home / "projects" / p / "src" / p / "knowledge-plane"
        kp.mkdir(parents=True)
        (kp / "INDEX.md").write_text(f"KP-{p}\n")
    for slug, pid in ids.items():
        if slug == "project2-eval":
            # No agent has started there yet: the Hub has made the project-config folder but no shared-dirs/.
            # The copy must still land, so the first agent already finds the knowledge plane.
            (home / "project-configs" / f"{slug}__{pid[:8]}").mkdir(parents=True)
        else:
            (home / "project-configs" / f"{slug}__{pid[:8]}" / "shared-dirs").mkdir(parents=True)
    stale = home / "project-configs" / "project1-eval__22222222" / "shared-dirs" / "knowledge-plane" / "old.md"
    stale.parent.mkdir()
    stale.write_text("stale")
    monkeypatch.setattr(watchdog, "SCION_HOME", str(home))
    monkeypatch.setattr(watchdog, "FACTORY_PROJECTS", list(ids))
    monkeypatch.setenv("KNOWLEDGE_PLANE_SOURCES", "project1=src/project1/knowledge-plane,"
                                                  "project2=src/project2/knowledge-plane")

    watchdog.check_shared_knowledge_plane(conn, apply=True)

    def kp(slug):
        return home / "project-configs" / f"{slug}__{ids[slug][:8]}" / "shared-dirs" / "knowledge-plane"
    assert (kp("project1") / "INDEX.md").read_text() == "KP-project1\n"
    assert (kp("project1-eval") / "INDEX.md").read_text() == "KP-project1\n"
    assert not (kp("project1-eval") / "old.md").exists()
    assert (kp("project2") / "INDEX.md").read_text() == "KP-project2\n"
    assert (kp("project2-eval") / "INDEX.md").read_text() == "KP-project2\n"


def test_sync_skips_a_project_the_hub_has_not_set_up_on_this_broker(watchdog, tmp_path, monkeypatch, capsys):
    home = tmp_path / ".scion"
    conn = _hub(home.mkdir() or home, [("11111111-aaaa", "project1")])
    kp = home / "projects" / "project1" / "src" / "project1" / "knowledge-plane"
    kp.mkdir(parents=True)
    (kp / "INDEX.md").write_text("KP\n")
    monkeypatch.setattr(watchdog, "SCION_HOME", str(home))
    monkeypatch.setattr(watchdog, "FACTORY_PROJECTS", ["project1"])
    monkeypatch.setenv("KNOWLEDGE_PLANE_SOURCES", "project1=src/project1/knowledge-plane")
    watchdog.check_shared_knowledge_plane(conn, apply=True)
    assert not (home / "project-configs").exists()
    assert "project1: skipped" in capsys.readouterr().out


def test_sync_reports_a_missing_source(watchdog, tmp_path, monkeypatch, capsys):
    home = tmp_path / ".scion"
    conn = _hub(home.mkdir() or home, [("11111111-aaaa", "project1")])
    monkeypatch.setattr(watchdog, "SCION_HOME", str(home))
    monkeypatch.setattr(watchdog, "FACTORY_PROJECTS", ["project1"])
    monkeypatch.setenv("KNOWLEDGE_PLANE_SOURCES", "project1=src/project1/knowledge-plane")
    watchdog.check_shared_knowledge_plane(conn, apply=True)
    assert "source missing" in capsys.readouterr().out


@pytest.mark.skipif(not any(os.access(os.path.join(p, "rsync"), os.X_OK)
                            for p in os.environ["PATH"].split(os.pathsep)), reason="rsync not installed")
def test_sync_reports_up_to_date_when_rsync_makes_no_changes(watchdog, tmp_path, monkeypatch, capsys):
    home = tmp_path / ".scion"
    conn = _hub(home.mkdir() or home, [("11111111-aaaa", "project1")])
    kp = home / "projects" / "project1" / "knowledge-plane"
    kp.mkdir(parents=True)
    (kp / "INDEX.md").write_text("KP\n")
    (home / "project-configs" / "project1__11111111" / "shared-dirs").mkdir(parents=True)
    monkeypatch.setattr(watchdog, "SCION_HOME", str(home))
    monkeypatch.setattr(watchdog, "FACTORY_PROJECTS", ["project1"])

    watchdog.check_shared_knowledge_plane(conn, apply=True)
    first_out = capsys.readouterr().out
    assert "synced" in first_out

    watchdog.check_shared_knowledge_plane(conn, apply=True)
    second_out = capsys.readouterr().out
    assert "up to date" in second_out


def test_watchdog_env_keeps_the_knowledge_plane_settings_for_cron(watchdog, monkeypatch):
    """install_cron writes watchdog.env for the cron runs; settings it leaves out silently fall back to defaults."""
    monkeypatch.setenv("KNOWLEDGE_PLANE_SOURCES", "project1=src/project1/knowledge-plane")
    monkeypatch.setenv("KNOWLEDGE_PLANE_SOURCE_PROJECT", "project1")
    monkeypatch.setenv("WATCHDOG_WAKE_ROLES", "lead,eval-lead,backend")
    monkeypatch.setenv("WATCHDOG_SUBPROCESS_TIMEOUT_SEC", "420")
    content = watchdog.watchdog_env_content("/usr/bin:/bin")
    assert "KNOWLEDGE_PLANE_SOURCES=project1=src/project1/knowledge-plane\n" in content
    assert "KNOWLEDGE_PLANE_SOURCE_PROJECT=project1\n" in content
    assert "WATCHDOG_WAKE_ROLES=lead,eval-lead,backend\n" in content
    assert "WATCHDOG_SUBPROCESS_TIMEOUT_SEC=420\n" in content
    assert "PATH=/usr/bin:/bin\n" in content


def test_run_on_hub_forwards_the_knowledge_plane_settings():
    text = (ROOT / "scripts" / "run-on-hub.sh").read_text()
    assert "KNOWLEDGE_PLANE_SOURCES" in text and "KNOWLEDGE_PLANE_SOURCE_PROJECT" in text
    assert "WATCHDOG_WAKE_ROLES" in text
    assert "WATCHDOG_SUBPROCESS_TIMEOUT_SEC" in text


def test_watchdog_projects_default_to_the_factory_env(watchdog):
    """Without FACTORY_PROJECTS the watchdog used to fall back to the reference factory's psearch projects."""
    assert watchdog.default_factory_projects({"FACTORY_PROJECTS": "a, b"}) == ["a", "b"]
    assert watchdog.default_factory_projects({"BUILD_PROJECT": "project1", "EVAL_PROJECT": "project1-eval"}) == [
        "project1", "project1-eval"]
    assert watchdog.default_factory_projects({}) == ["psearch", "psearch-eval"]


# --- templates: the knowledge plane path follows FACTORY_SUBDIR --------------------------------------------------

def _render(tmp_path, **extra):
    sys.path.insert(0, str(ROOT / "scripts"))
    import scion_factory

    spec = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")
    if extra.get("FACTORY_SUBDIR") or extra.get("AGENT_PREFIX"):
        spec["monorepo"] = {
            "subdir": extra.get("FACTORY_SUBDIR", "."),
            "agent_prefix": extra.get("AGENT_PREFIX", ""),
        }
    kit = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, kit)
    env = tmp_path / "factory.env"
    env.write_text((ROOT / "config" / "factory.env.example").read_text()
                   + "\nSPANNER_INSTANCE=i\nSPANNER_DATABASE=d\n" + "".join(f"{k}={v}\n" for k, v in extra.items()))
    out = tmp_path / "out"
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "render-templates.py"), "--env", str(env),
                          "--src", str(kit / "templates"), "--out", str(out)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*") if p.is_file()}


def test_knowledge_plane_path_follows_the_product_folder(tmp_path):
    files = _render(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    assert "`/workspace/src/project1/knowledge-plane/INDEX.md`" in files["lead/system-prompt.md"]
    assert not [p for p, t in files.items() if "/workspace/knowledge-plane" in t]


def test_knowledge_plane_path_is_unchanged_for_a_single_factory(tmp_path):
    files = _render(tmp_path)
    assert "`/workspace/knowledge-plane/INDEX.md`" in files["lead/system-prompt.md"]


def test_every_agent_learns_about_the_shared_directories(tmp_path):
    files = _render(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    agents = files["lead/agents.md"]
    assert "/scion-volumes/scratchpad" in agents
    assert "/scion-volumes/knowledge-plane" in agents


def test_rendered_prompts_and_configs_have_no_placeholders_left(tmp_path):
    """A variable missing from render-templates.py VARS is left as ${NAME}: an agent would read it literally."""
    import re
    files = _render(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    leftovers = {f"{p}: {m}" for p, t in files.items() if p.endswith((".md", ".yaml"))
                 for m in re.findall(r"\$\{[A-Z][A-Z0-9_]*\}", t)}
    assert leftovers == set()


def test_shared_agents_template_forbids_stash_untracked_and_scopes_staging(tmp_path):
    files = _render(tmp_path)
    agents = files["lead/agents.md"]
    assert "git stash push -u" not in agents
    assert "git stash -u" in agents  # explicitly listed as forbidden
    for needle in (
        "git add --",
        "git diff --cached --name-only",
        ".scion/",
        ".agents/",
        "rev-list --count",
        "/scion-volumes/scratchpad/pending-",
    ):
        assert needle in agents, f"missing {needle!r} in rendered lead/agents.md"


def test_project_create_waits_long_enough_for_a_large_clone(monkeypatch, tmp_path):
    """A shared project is cloned inside the create request; cloning a large monorepo (kubernetes: ~1.3 GB) takes
    minutes, so the API call must not give up after the default 60 seconds."""
    seen = {}

    class Resp:
        status = 201
        def read(self): return b'{"project": {"id": "p1"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(admin, "_token", lambda: "t")
    monkeypatch.setattr(admin, "_TOKEN", None)
    monkeypatch.setattr(admin.urllib.request, "urlopen", lambda req, timeout: seen.update(timeout=timeout) or Resp())
    monkeypatch.setattr(admin, "_db", lambda: type("C", (), {"execute": lambda *a: type("R", (), {"fetchone": lambda s: None})()})())
    tok = tmp_path / "tok"
    tok.write_text("x")
    args = type("A", (), {"slug": "kubelet", "git_url": "https://github.com/o/k.git", "branch": "factory/main",
                          "mode": "shared", "token_file": str(tok)})()
    admin.cmd_project_create(args)
    assert seen["timeout"] >= 1800


def _hub_home(tmp_path, slug=None, pid="p1", seeded=True):
    """A fake ~/.scion: hub.db with a projects table, and the shared checkout's .scion/project-id when seeded."""
    home = tmp_path / "scion-home"
    home.mkdir()
    db = sqlite3.connect(home / "hub.db")
    db.execute("CREATE TABLE projects (id TEXT, slug TEXT)")
    if slug:
        db.execute("INSERT INTO projects VALUES (?, ?)", (pid, slug))
        if seeded:
            (home / "projects" / slug / ".scion").mkdir(parents=True)
            (home / "projects" / slug / ".scion" / "project-id").write_text(pid + "\n")
    db.commit()
    db.close()
    return home


def _create_with_disconnect(monkeypatch, tmp_path, home, capsys):
    """The Hub's 60 s write timeout closes the connection while the clone keeps running (L28 Kubernetes run)."""
    import http.client

    monkeypatch.setattr(admin, "SCION_HOME", str(home))
    monkeypatch.setattr(admin, "HUB_DB", str(home / "hub.db"))
    monkeypatch.setattr(admin, "_token", lambda: "t")
    monkeypatch.setattr(admin, "_TOKEN", None)
    monkeypatch.setattr(admin.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def urlopen(req, timeout):
        calls["n"] += 1
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(admin.urllib.request, "urlopen", urlopen)
    tok = tmp_path / "tok"
    tok.write_text("x")
    args = type("A", (), {"slug": "kubelet", "git_url": "https://github.com/o/k.git", "branch": "factory/main",
                          "mode": "shared", "token_file": str(tok), "wait": 5})()
    try:
        admin.cmd_project_create(args)
        code = 0
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out, calls["n"]


def test_project_create_reports_a_clone_that_finished_after_the_hub_dropped_the_connection(monkeypatch, tmp_path, capsys):
    # The DB starts empty (the slug check passes); the clone then finishes on the Hub.
    home = _hub_home(tmp_path)
    real_db = admin._db
    state = {"checks": 0}

    def db():
        state["checks"] += 1
        if state["checks"] == 2:  # first poll after the disconnect: the Hub has seeded the checkout
            conn = sqlite3.connect(home / "hub.db")
            conn.execute("INSERT INTO projects VALUES ('p1', 'kubelet')")
            conn.commit()
            (home / "projects" / "kubelet" / ".scion").mkdir(parents=True)
            (home / "projects" / "kubelet" / ".scion" / "project-id").write_text("p1\n")
        return real_db()

    monkeypatch.setattr(admin, "_db", db)
    code, out, posts = _create_with_disconnect(monkeypatch, tmp_path, home, capsys)
    assert code == 0, out
    assert posts == 1  # never re-POSTs: a second create would race the running clone
    assert "kubelet: created id=p1" in out
    assert "write timeout" in out


def test_project_create_fails_when_the_hub_rolled_the_project_back(monkeypatch, tmp_path, capsys):
    home = _hub_home(tmp_path)  # the row never appears: the clone failed and the Hub rolled back
    code, out, _ = _create_with_disconnect(monkeypatch, tmp_path, home, capsys)
    assert code not in (0, None)
