"""Monorepo multi-factory support: agent names prefixed with the factory slug and a per-factory start folder.

Spec: docs/plans/2026-10-22-monorepo-factories.md
"""
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
ROLES = sorted(d.name for d in TEMPLATES.iterdir() if d.is_dir() and not d.name.startswith("_"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_templates = _load("render_templates", ROOT / "scripts" / "render-templates.py")

EXAMPLE_ENV = (ROOT / "config" / "factory.env.example").read_text()


def _env_file(tmp_path, **extra):
    """factory.env.example plus the optional values the reference templates need, plus `extra`."""
    lines = [EXAMPLE_ENV, "SPANNER_INSTANCE=psearch-instance", "SPANNER_DATABASE=psearch-db"]
    lines += [f"{k}={v}" for k, v in extra.items()]
    path = tmp_path / "factory.env"
    path.write_text("\n".join(lines) + "\n")
    return path


def _run_render(tmp_path, *args, **env_values):
    env_path = _env_file(tmp_path, **env_values)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render-templates.py"), "--env", str(env_path), *args],
        capture_output=True, text=True,
    )


# --- Task 1: naming and sub-folder variables -------------------------------------------------------------------

def test_agent_name_prefixes_role_with_factory_slug():
    assert render_templates.agent_name("project1", "eval-lead") == "project1-eval-lead"


def test_agent_name_without_prefix_is_the_bare_role():
    assert render_templates.agent_name("", "lead") == "lead"


def test_role_agent_vars_derive_one_variable_per_role():
    assert render_templates.role_agent_vars("project1", ["lead", "eval-lead"]) == {
        "LEAD_AGENT": "project1-lead",
        "EVAL_LEAD_AGENT": "project1-eval-lead",
    }


@pytest.mark.parametrize("prefix", ["Project1", "project_1", "project1-", "-project1", "project--1"])
def test_validate_prefix_rejects_non_slugs(prefix):
    with pytest.raises(ValueError, match="AGENT_PREFIX"):
        render_templates.validate_prefix(prefix, ["lead"])


def test_validate_prefix_rejects_names_longer_than_scion_allows():
    with pytest.raises(ValueError, match="63"):
        render_templates.validate_prefix("p" * 50, ["search-evaluator"])


def test_validate_prefix_accepts_a_full_slug():
    render_templates.validate_prefix("project1", ROLES)


@pytest.mark.parametrize("value,expected", [("src/project1/", "src/project1"), ("", "."), (".", "."),
                                            ("./src/project1", "src/project1")])
def test_normalize_subdir(value, expected):
    assert render_templates.normalize_subdir(value) == expected


@pytest.mark.parametrize("value", ["../x", "/abs", "src/../x"])
def test_normalize_subdir_rejects_paths_outside_the_repo(value):
    with pytest.raises(ValueError, match="FACTORY_SUBDIR"):
        render_templates.normalize_subdir(value)


def test_cli_agent_names_lists_prefixed_names(tmp_path):
    res = _run_render(tmp_path, "--agent-names", AGENT_PREFIX="project1")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert "lead\tproject1-lead" in lines
    assert "eval-lead\tproject1-eval-lead" in lines
    assert len(lines) == len(ROLES)


def test_cli_rejects_an_invalid_prefix(tmp_path):
    res = _run_render(tmp_path, "--agent-names", AGENT_PREFIX="Project_1")
    assert res.returncode != 0
    assert "AGENT_PREFIX" in res.stderr


def test_cli_rejects_a_subdir_outside_the_repo(tmp_path):
    res = _run_render(tmp_path, "--out", str(tmp_path / "out"), FACTORY_SUBDIR="../elsewhere")
    assert res.returncode != 0
    assert "FACTORY_SUBDIR" in res.stderr


# --- Task 2: templates use the role variables and carry FACTORY_SUBDIR -------------------------------------------

ROLE_ALT = "|".join(re.escape(r) for r in sorted(ROLES, key=len, reverse=True))
BARE_MESSAGE = re.compile(rf"scion message ({ROLE_ALT})\b(?!-)")
BARE_REPORT = re.compile(rf"report to `({ROLE_ALT})`", re.IGNORECASE)


def _render_all(tmp_path, **env_values):
    out = tmp_path / "out"
    res = _run_render(tmp_path, "--out", str(out), **env_values)
    assert res.returncode == 0, res.stderr
    return {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*") if p.is_file()}


def test_prefixed_render_leaves_no_bare_peer_names(tmp_path):
    files = _render_all(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    offenders = [f"{path}: {m.group(0)}" for path, text in files.items()
                 for pattern in (BARE_MESSAGE, BARE_REPORT) for m in pattern.finditer(text)]
    assert offenders == []


def test_prefixed_render_names_peers_with_the_factory_slug(tmp_path):
    files = _render_all(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    lead = files["lead/system-prompt.md"]
    assert "`project1-reviewer`" in lead
    assert "`project1-deployer`" in lead
    assert "scion message project1-eval-lead" in files["search-evaluator/system-prompt.md"]
    assert "You report to `project1-lead`" in files["backend/system-prompt.md"]


def test_every_rendered_template_carries_the_start_folder(tmp_path):
    files = _render_all(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1/")
    for role in ROLES:
        assert 'FACTORY_SUBDIR: "src/project1"' in files[f"{role}/scion-agent.yaml"], role
    assert "`src/project1`" in files["lead/agents.md"]


def test_unprefixed_render_keeps_bare_role_names(tmp_path):
    files = _render_all(tmp_path)
    assert "scion message lead" in files["backend/system-prompt.md"]
    assert "You report to `lead`" in files["backend/system-prompt.md"]
    for role in ROLES:
        assert 'FACTORY_SUBDIR: "."' in files[f"{role}/scion-agent.yaml"], role


def test_rendered_scion_agent_yaml_stays_valid_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    files = _render_all(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    for role in ROLES:
        doc = yaml.safe_load(files[f"{role}/scion-agent.yaml"])
        assert doc["env"]["FACTORY_SUBDIR"] == "src/project1", role


# --- Task 3: factory-cd.sh launcher ------------------------------------------------------------------------------

FACTORY_CD = ROOT / "config" / "harness" / "home" / ".local" / "bin" / "factory-cd.sh"


@pytest.fixture
def monorepo(tmp_path):
    repo = tmp_path / "repo"
    for product in ("project1", "project2", "project3"):
        (repo / "src" / product).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    return repo, home


def _factory_cd(cwd, home, subdir, *cmd):
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    if subdir is not None:
        env["FACTORY_SUBDIR"] = subdir
    return subprocess.run(["sh", str(FACTORY_CD), *(cmd or ("sh", "-c", "pwd -P"))],
                          cwd=cwd, env=env, capture_output=True, text=True)


def test_factory_cd_starts_the_harness_in_the_product_folder(monorepo):
    repo, home = monorepo
    res = _factory_cd(repo, home, "src/project1")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str((repo / "src" / "project1").resolve())


def test_factory_cd_resolves_against_the_repo_root_from_a_nested_cwd(monorepo):
    repo, home = monorepo
    res = _factory_cd(repo / "src" / "project3", home, "src/project2")
    assert res.stdout.strip() == str((repo / "src" / "project2").resolve()), res.stderr


@pytest.mark.parametrize("subdir", [None, "", "."])
def test_factory_cd_without_a_subdir_keeps_the_cwd(monorepo, subdir):
    repo, home = monorepo
    res = _factory_cd(repo, home, subdir)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(repo.resolve())


def test_factory_cd_passes_arguments_through_unchanged(monorepo):
    repo, home = monorepo
    res = _factory_cd(repo, home, "src/project1", "sh", "-c", 'printf "%s|" "$@"', "sh", "a b", "--flag")
    assert res.stdout == "a b|--flag|"


@pytest.mark.parametrize("subdir", ["/etc", "../outside", "src/../../x"])
def test_factory_cd_refuses_paths_outside_the_repo(monorepo, subdir):
    repo, home = monorepo
    res = _factory_cd(repo, home, subdir)
    assert res.returncode == 64
    assert "FACTORY_SUBDIR" in res.stderr


def test_factory_cd_refuses_a_missing_folder(monorepo):
    repo, home = monorepo
    res = _factory_cd(repo, home, "src/project9")
    assert res.returncode == 66
    assert "src/project9" in res.stderr


def test_factory_cd_outside_git_resolves_against_the_cwd(tmp_path):
    (tmp_path / "work" / "src" / "project1").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    res = _factory_cd(tmp_path / "work", tmp_path / "home", "src/project1")
    assert res.stdout.strip() == str((tmp_path / "work" / "src" / "project1").resolve()), res.stderr


def test_factory_cd_pre_trusts_the_folder_for_claude(monorepo):
    repo, home = monorepo
    (home / ".claude.json").write_text(json.dumps({"projects": {"/workspace": {"hasTrustDialogAccepted": True}},
                                                   "keep": 1}))
    for _ in range(2):
        assert _factory_cd(repo, home, "src/project1").returncode == 0
    cfg = json.loads((home / ".claude.json").read_text())
    target = str((repo / "src" / "project1").resolve())
    assert cfg["projects"][target]["hasTrustDialogAccepted"] is True
    assert cfg["projects"]["/workspace"] == {"hasTrustDialogAccepted": True}
    assert cfg["keep"] == 1


def test_factory_cd_pre_trusts_the_folder_for_antigravity_once(monorepo):
    repo, home = monorepo
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"trustedWorkspaces": ["/workspace"], "onboardingComplete": True}))
    for _ in range(2):
        assert _factory_cd(repo, home, "src/project1").returncode == 0
    data = json.loads(settings.read_text())
    assert data["trustedWorkspaces"] == ["/workspace", str((repo / "src" / "project1").resolve())]
    assert data["onboardingComplete"] is True


def test_factory_cd_leaves_absent_harness_configs_absent(monorepo):
    repo, home = monorepo
    assert _factory_cd(repo, home, "src/project1").returncode == 0
    assert not (home / ".claude.json").exists()
    assert not (home / ".gemini").exists()


def test_factory_cd_carries_antigravity_hooks_into_the_folder(monorepo):
    repo, home = monorepo
    (repo / ".agents").mkdir()
    (repo / ".agents" / "hooks.json").write_text('{"hooks": {}}')
    for _ in range(2):
        assert _factory_cd(repo, home, "src/project1").returncode == 0
    assert (repo / "src" / "project1" / ".agents" / "hooks.json").read_text() == '{"hooks": {}}'
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(".agents/") == 1


def test_factory_cd_refreshes_stale_antigravity_hooks(monorepo):
    repo, home = monorepo
    (repo / ".agents").mkdir()
    (repo / ".agents" / "hooks.json").write_text('{"v": 1}')
    assert _factory_cd(repo, home, "src/project1").returncode == 0
    (repo / ".agents" / "hooks.json").write_text('{"v": 2}')
    assert _factory_cd(repo, home, "src/project1").returncode == 0
    assert (repo / "src" / "project1" / ".agents" / "hooks.json").read_text() == '{"v": 2}'


# --- Task 5: watchdog lead discovery -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def watchdog(tmp_path_factory):
    os.environ.setdefault("SCION_HOME", str(tmp_path_factory.mktemp("scion-home")))
    return _load("factory_watchdog", ROOT / "scripts" / "factory-watchdog.py")


@pytest.mark.parametrize("project,agents,expected", [
    ("project1", ["project1-backend", "project1-lead"], "project1-lead"),
    ("project1-eval", ["project1-factory-analyst", "project1-eval-lead"], "project1-eval-lead"),
    ("psearch", ["backend", "lead"], "lead"),
    ("psearch-eval", ["factory-analyst", "eval-lead"], "eval-lead"),
    ("project2", ["project2-backend"], "lead"),
])
def test_lead_agent_for_follows_the_naming_convention(watchdog, project, agents, expected):
    assert watchdog.lead_agent_for(project, agents) == expected


def test_lead_agent_for_never_picks_another_factorys_lead(watchdog):
    assert watchdog.lead_agent_for("project2", ["project1-lead", "project2-lead"]) == "project2-lead"


# --- Task 4: <harness>-factory harness-configs -------------------------------------------------------------------

make_hc = _load("make_factory_harness_configs", ROOT / "scripts" / "make-factory-harness-configs.py")

CLAUDE_CONFIG = """harness: claude
image: scion-claude:latest
command:
  base: ["claude", "--no-chrome", "--dangerously-skip-permissions"]
  resume_flag: "--continue"
  task_position: after_base_args
"""
OPENCODE_CONFIG = """harness: opencode
command:
  # opencode-launch.sh renames GITHUB_TOKEN
  base: ["sh", "/home/scion/.local/bin/opencode-launch.sh"]
  resume_flag: "--continue"
"""
LAUNCHER = ["sh", "/home/scion/.local/bin/factory-cd.sh"]


def _base(text):
    return json.loads(re.search(r"^\s+base:\s*(\[.*\])\s*$", text, re.M).group(1))


def test_prefix_command_base_puts_factory_cd_first():
    text, changed = make_hc.prefix_command_base(CLAUDE_CONFIG)
    assert changed is True
    assert _base(text) == LAUNCHER + ["claude", "--no-chrome", "--dangerously-skip-permissions"]
    assert text.replace(text.splitlines()[3], "") == CLAUDE_CONFIG.replace(CLAUDE_CONFIG.splitlines()[3], "")


def test_prefix_command_base_is_idempotent():
    once, _ = make_hc.prefix_command_base(CLAUDE_CONFIG)
    twice, changed = make_hc.prefix_command_base(once)
    assert changed is False
    assert twice == once


def test_prefix_command_base_wraps_an_existing_launcher():
    text, _ = make_hc.prefix_command_base(OPENCODE_CONFIG)
    assert _base(text) == LAUNCHER + ["sh", "/home/scion/.local/bin/opencode-launch.sh"]
    assert "# opencode-launch.sh renames GITHUB_TOKEN" in text


def test_prefix_command_base_requires_a_command_base():
    with pytest.raises(ValueError, match="command.base"):
        make_hc.prefix_command_base("harness: x\nimage: y\n")


@pytest.mark.parametrize("subdir,expected", [("src/project1", "claude-factory"), (".", "claude")])
def test_harness_vars_switch_to_factory_configs_only_with_a_subdir(subdir, expected):
    assert make_hc.harness_vars(subdir)["CLAUDE_HARNESS"] == expected
    assert set(make_hc.harness_vars(subdir)) == {"CLAUDE_HARNESS", "ANTIGRAVITY_HARNESS", "OPENCODE_HARNESS",
                                                 "HERMES_HARNESS"}


@pytest.fixture
def scion_home(tmp_path):
    home = tmp_path / ".scion"
    for name, text in (("claude", CLAUDE_CONFIG), ("opencode", OPENCODE_CONFIG)):
        hc = home / "harness-configs" / name
        (hc / "home" / ".claude").mkdir(parents=True)
        (hc / "config.yaml").write_text(text)
        (hc / "provision.py").write_text("# provisioner\n")
    return home


def _make_hc(scion_home, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "make-factory-harness-configs.py"),
                           "--scion-home", str(scion_home), "--harness", "claude,opencode", *args],
                          capture_output=True, text=True)


def test_make_factory_harness_configs_dry_run_writes_nothing(scion_home):
    res = _make_hc(scion_home)
    assert res.returncode == 0, res.stderr
    assert "claude-factory" in res.stdout
    assert not (scion_home / "harness-configs" / "claude-factory").exists()


def test_make_factory_harness_configs_apply_builds_custom_copies(scion_home):
    for _ in range(2):
        res = _make_hc(scion_home, "--apply", "--no-sync")
        assert res.returncode == 0, res.stderr
    for name, original in (("claude", CLAUDE_CONFIG), ("opencode", OPENCODE_CONFIG)):
        copy = scion_home / "harness-configs" / f"{name}-factory"
        assert _base((copy / "config.yaml").read_text())[:2] == LAUNCHER
        assert (copy / "provision.py").read_text() == "# provisioner\n"
        launcher = copy / "home" / ".local" / "bin" / "factory-cd.sh"
        assert launcher.read_text() == FACTORY_CD.read_text()
        assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
        assert (scion_home / "harness-configs" / name / "config.yaml").read_text() == original


def test_make_factory_harness_configs_skips_missing_harnesses(scion_home):
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "make-factory-harness-configs.py"),
                          "--scion-home", str(scion_home), "--harness", "hermes", "--apply", "--no-sync"],
                         capture_output=True, text=True)
    assert res.returncode != 0
    assert "hermes" in res.stderr


def test_make_factory_harness_configs_via_hub_ships_the_launcher_inside_the_script(scion_home, tmp_path):
    """run-on-hub.sh copies only the script to the VM, so --via-hub must embed factory-cd.sh in it."""
    fake_hub = tmp_path / "fake-run-on-hub.sh"
    # Like run-on-hub.sh: run the script from a temp copy outside the repository.
    fake_hub.write_text('#!/bin/sh\nd=$(mktemp -d)\ncp "$1" "$d/remote.py"\nshift\nexec python3 "$d/remote.py" "$@"\n')
    fake_hub.chmod(0o755)
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "make-factory-harness-configs.py"), "--via-hub",
                          "--scion-home", str(scion_home), "--harness", "claude", "--apply", "--no-sync"],
                         capture_output=True, text=True, env={**os.environ, "RUN_ON_HUB": str(fake_hub)})
    assert res.returncode == 0, res.stderr
    launcher = scion_home / "harness-configs" / "claude-factory" / "home" / ".local" / "bin" / "factory-cd.sh"
    assert launcher.read_text() == FACTORY_CD.read_text()


def _fake_hub(scion_home, secret="s3cret"):
    import sqlite3
    conn = sqlite3.connect(scion_home / "hub.db")
    conn.execute("CREATE TABLE users (id TEXT, email TEXT, role TEXT)")
    conn.execute("INSERT INTO users VALUES ('u1', 'po@example.com', 'admin')")
    conn.commit()
    conn.close()
    (scion_home / "hub.env").write_text(f"OTHER=1\nSCION_SERVER_SESSION_SECRET={secret}\n")


def test_cli_env_mints_an_admin_token_for_the_scion_cli(scion_home):
    """`scion harness-config sync --global` needs Hub admin; on the VM the CLI accepts the admin JWT as SCION_HUB_TOKEN."""
    import base64, hashlib, hmac
    _fake_hub(scion_home)
    env = make_hc.cli_env(str(scion_home), "po@example.com", base={"PATH": "/bin"})
    assert env["SCION_HUB_ENDPOINT"] == "http://localhost:8080"
    header, payload, signature = env["SCION_HUB_TOKEN"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert (claims["email"], claims["role"], claims["uid"]) == ("po@example.com", "admin", "u1")
    key = hashlib.sha256(b"scion-hub-signing-key:user_signing_key:s3cret").digest()
    expected = base64.urlsafe_b64encode(hmac.new(key, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    assert signature == expected.rstrip(b"=").decode()


def test_cli_env_keeps_an_existing_token(scion_home):
    env = make_hc.cli_env(str(scion_home), "po@example.com", base={"SCION_HUB_TOKEN": "given"})
    assert env["SCION_HUB_TOKEN"] == "given"


def test_cli_env_fails_clearly_for_an_unknown_user(scion_home):
    _fake_hub(scion_home)
    with pytest.raises(SystemExit, match="nobody@example.com"):
        make_hc.cli_env(str(scion_home), "nobody@example.com", base={})


def test_rendered_templates_use_factory_harness_configs_with_a_subdir(tmp_path):
    files = _render_all(tmp_path, AGENT_PREFIX="project1", FACTORY_SUBDIR="src/project1")
    assert "default_harness_config: claude-factory" in files["lead/scion-agent.yaml"]
    assert "default_harness_config: antigravity-factory" in files["eval-lead/scion-agent.yaml"]


def test_rendered_templates_keep_bundled_harness_configs_without_a_subdir(tmp_path):
    files = _render_all(tmp_path)
    assert "default_harness_config: claude\n" in files["lead/scion-agent.yaml"]
    assert "default_harness_config: opencode\n" in files["backend/scion-agent.yaml"]


@pytest.mark.parametrize("config,expected", [("claude-factory", "claude"), ("claude", "claude"),
                                             ("antigravity-factory", "antigravity"), ("my-hermes", "my-hermes")])
def test_push_templates_registers_the_harness_type_not_the_config_name(config, expected):
    push = _load("push_templates", ROOT / "scripts" / "push-templates.py")
    assert push.harness_type(config) == expected


def test_factory_cd_switches_google_managed_direct_iap_to_metadata_access_token(monorepo):
    import http.server
    import threading

    repo, home = monorepo

    class _MetaHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Metadata-Flavor") != "Google":
                self.send_response(403)
                self.end_headers()
                return
            if (
                self.path.startswith("/computeMetadata/v1/instance/service-accounts/default/token?scopes=")
                and "userinfo.email" in self.path
                and "openid" in self.path
            ):
                body = json.dumps({"access_token": "ya29-pod-direct-iap-token", "token_type": "Bearer"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _MetaHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # Even without FACTORY_SUBDIR (single-repo GKE pod), Direct IAP access token must be wired!
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "SCION_TRANSPORT_MODE": "iap",
            "SCION_TRANSPORT_AUDIENCE": "369001918367-syntheticdirectiap.apps.googleusercontent.com",
            "SCION_TRANSPORT_TOKEN": "eyJ-stale-oidc-jwt-rejected-by-direct-iap",
            "SCION_METADATA_URL": f"http://127.0.0.1:{srv.server_port}",
            "FACTORY_CD_IAP_REFRESH_SEC": "0",
        }
        res = subprocess.run(
            [
                "sh",
                str(FACTORY_CD),
                "python3",
                "-c",
                "import json, os; print(json.dumps({'file': os.environ.get('SCION_TRANSPORT_TOKEN_FILE'), 'boot': os.environ.get('SCION_TRANSPORT_TOKEN')}))",
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        got = json.loads(res.stdout.strip())
        assert got["boot"] is None
        assert got["file"] == str(home / ".scion" / "transport-token")
        assert Path(got["file"]).read_text().strip() == "ya29-pod-direct-iap-token"
    finally:
        srv.shutdown()

