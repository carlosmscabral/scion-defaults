"""A compiled factory kit deploys with the same scripts as the reference factory (README end-to-end quickstart).

The kit's config/factory.env.example must use the variable names that run-on-hub.sh, render-templates.py and
push-templates.py read, and push-templates.py must take the kit's templates directory.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scion_factory  # noqa: E402

SCRIPT_VARS = ("HUB_PROJECT HUB_ZONE HUB_VM HUB_OS_USER HUB_LOCAL_URL SANDBOX_PROJECT SANDBOX_REGION FIRESTORE_PROJECT "
               "FIRESTORE_DATABASE DEPLOYER_SA_NAME RUNTIME_SA_NAME GITHUB_REPO BUILD_PROJECT EVAL_PROJECT BUILD_BRANCH "
               "EVAL_BRANCH PO_EMAIL FACTORY_PROJECTS SERVING_SERVICE DASHBOARD_SERVICE DATA_BUCKET BQ_DATASET").split()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    out = tmp_path_factory.mktemp("kit")
    scion_factory.compile_factory_boilerplate(scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml"),
                                              out)
    return out


def _env_keys(text):
    keys = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            keys[key.strip()] = value.strip().strip('"')
    return keys


def test_kit_env_uses_the_script_variable_names(kit):
    keys = _env_keys((kit / "config" / "factory.env.example").read_text())
    assert set(SCRIPT_VARS) <= set(keys), sorted(set(SCRIPT_VARS) - set(keys))
    assert keys["BUILD_PROJECT"] == "pulse-ledger"
    assert keys["EVAL_PROJECT"] == "pulse-ledger-eval"
    assert keys["FACTORY_PROJECTS"] == "pulse-ledger,pulse-ledger-eval"


def test_kit_env_has_no_export_prefix(kit):
    """render-templates.py reads KEY=value lines; `export KEY=value` would silently drop every value."""
    lines = (kit / "config" / "factory.env.example").read_text().splitlines()
    assert [line for line in lines if line.startswith("export ")] == []


def test_kit_verify_script_reads_build_project(kit):
    text = (kit / "scripts" / "verify-factory.sh").read_text()
    assert "${BUILD_PROJECT:-pulse-ledger}" in text


def _fake_gh(tmp_path):
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    gh.chmod(0o755)
    return log


def test_seed_waves_creates_issues_with_dry_run_0(kit, tmp_path):
    log = _fake_gh(tmp_path)
    import os
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "DRY_RUN": "0",
           "GITHUB_REPO": "example-org/ledger"}
    res = subprocess.run(["bash", str(kit / "scripts" / "seed-github-waves.sh")], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    created = [line for line in log.read_text().splitlines() if line.startswith("issue create")]
    assert created and all("--repo example-org/ledger" in line for line in created)


def test_seed_waves_skips_already_existing_wave_issues_on_rerun(kit, tmp_path):
    import json
    import os

    waves = json.loads((kit / "waves" / "issues.json").read_text())
    existing_title = waves[0]["issues"][0]["title"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh.log"
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        'if [[ "$1 $2" == "issue list" && "$*" == *"--json title"* ]]; then\n'
        f"  printf '[%s]\\n' '{json.dumps({'title': existing_title})}'\n"
        "fi\n"
    )
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "DRY_RUN": "0", "GITHUB_REPO": "example-org/ledger"}
    res = subprocess.run(["bash", str(kit / "scripts" / "seed-github-waves.sh")], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    created = [line for line in log.read_text().splitlines() if line.startswith("issue create") and existing_title in line]
    assert created == [], f"Expected existing wave issue to be skipped on rerun, got: {created}"


def test_seed_waves_is_a_dry_run_by_default(kit, tmp_path):
    log = _fake_gh(tmp_path)
    import os
    env = {k: v for k, v in os.environ.items() if k != "DRY_RUN"}
    env["PATH"] = f"{tmp_path / 'bin'}:{os.environ['PATH']}"
    res = subprocess.run(["bash", str(kit / "scripts" / "seed-github-waves.sh")], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "issue create" not in (log.read_text() if log.exists() else "")


def test_kit_templates_render_with_the_kit_env(kit, tmp_path):
    """The path push-templates.py takes: render-templates.py --src <kit>/templates with the kit's env file."""
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "render-templates.py"),
                          "--env", str(kit / "config" / "factory.env.example"), "--src", str(kit / "templates"),
                          "--out", str(tmp_path / "out")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "out" / "domain-evaluator" / "system-prompt.md").exists()
    assert (tmp_path / "out" / "lead" / "agents.md").read_text() == (kit / "templates" / "lead" / "agents.md").read_text()


def test_default_roster_uses_only_harnesses_that_run_unpatched():
    """Full e2e on nightly-20261022: the bundled Hermes config passes --create-if-missing, which the Hermes CLI in a
    fresh image rejects (crash, exit 2), and the bundled OpenCode launcher stops at a permission prompt. Claude Code
    and Antigravity ran without patches, so the draft roster defaults to them; the interview can still pick others."""
    draft = scion_factory.draft_spec_from_repo(None)
    harnesses = {r["harness"] for r in draft["team"]["build_roles"] + draft["team"]["eval_roles"]}
    assert harnesses <= {"claude", "antigravity"}


def test_inspect_detects_a_requirements_txt_python_service(tmp_path):
    """Found in the full e2e: a FastAPI repo with only requirements.txt came out as language `unknown`."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.115.0\ngoogle-cloud-firestore==2.19.0\n"
                                               "google-cloud-storage==2.18.0\n")
    detected = scion_factory.inspect_repository(tmp_path)["detected"]
    assert detected["primary_language"] == "python"
    assert detected["test_command"] == "pytest -q"
    assert detected["framework"] == "FastAPI"
    assert detected["cloud_stores"] == ["firestore", "gcs"]


def test_push_templates_renders_from_a_kit_directory():
    push = _load("push_templates_kit", ROOT / "scripts" / "push-templates.py")
    cmd = push.render_command("/tmp/out", ["lead", "backend"], "/kits/ledger/templates")
    assert cmd[1:] == [str(ROOT / "scripts" / "render-templates.py"), "--out", "/tmp/out", "--roles", "lead,backend",
                       "--src", "/kits/ledger/templates"]
    with pytest.raises(ValueError, match="--templates"):
        push.render_command("/tmp/out", ["lead"], "")


def test_scion_admin_runs_the_scion_cli_as_the_admin(monkeypatch):
    """`scion-admin.py scion <args>` runs the scion CLI on the VM with the PO's admin token (never printed)."""
    admin = _load("scion_admin_kit", ROOT / "scripts" / "scion-admin.py")
    monkeypatch.setattr(admin, "_token", lambda: "minted")
    ran = {}
    monkeypatch.setattr(admin.subprocess, "run", lambda cmd, env: ran.update(cmd=cmd, env=env) or
                        type("R", (), {"returncode": 0})())
    with pytest.raises(SystemExit) as exit_info:
        admin.cmd_scion(type("A", (), {"args": ["start", "lead", "-t", "lead", "--project=ledger", "do it"]})())
    assert exit_info.value.code == 0
    assert ran["cmd"] == ["scion", "start", "lead", "-t", "lead", "--project=ledger", "do it"]
    assert ran["env"]["SCION_HUB_TOKEN"] == "minted"
    assert ran["env"]["SCION_HUB_ENDPOINT"] == admin.HUB_URL
