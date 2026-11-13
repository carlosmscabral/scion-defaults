#!/usr/bin/env python3
"""Upgrade OpenCode and Hermes agent harnesses to Gemini 4 Argon on the Hub VM.

Usage:
  scripts/run-on-hub.sh docs/scion/scripts/patch-opencode-and-hermes.py [--apply]
"""
import argparse
import glob
import json
import os
import sys

SCION_HOME = os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion"))

OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "model": "google-vertex/gemini-4-argon",
    "small_model": "google-vertex/gemini-3.8-flash",
    "disabled_providers": ["google-vertex-anthropic", "opencode"],
    "provider": {
        "google-vertex": {
            "models": {
                "gemini-4-argon": {
                    "id": "gemini-4-argon",
                    "name": "Gemini 4 Argon",
                    "tool_call": True,
                    "reasoning": True,
                    "attachment": True,
                    "temperature": True,
                    "limit": {
                        "context": 1048576,
                        "output": 65536,
                    },
                },
                "gemini-3.8-flash": {
                    "id": "gemini-3.8-flash",
                    "name": "Gemini 3.8 Flash",
                    "tool_call": True,
                    "reasoning": True,
                    "attachment": True,
                    "temperature": True,
                    "limit": {
                        "context": 1048576,
                        "output": 65536,
                    },
                },
            }
        }
    },
}

OPENCODE_LAUNCH_SCRIPT = """#!/bin/sh
PROJECT="${GOOGLE_CLOUD_PROJECT:-${HUB_PROJECT:-}}"
export GOOGLE_CLOUD_PROJECT="${PROJECT}"
export GOOGLE_CLOUD_LOCATION=global
export GOOGLE_CLOUD_REGION=global
export GOOGLE_VERTEX_PROJECT="${PROJECT}"
export GOOGLE_VERTEX_LOCATION=global
export VERTEXAI_PROJECT="${PROJECT}"
export VERTEX_LOCATION=global

mkdir -p "$HOME/.config/opencode" "$HOME/.local/state/opencode" "$HOME/.cache/opencode"
rm -f "$HOME/.config/opencode/.opencode.json"

cat << 'JSON' > "$HOME/.cache/opencode/models.json"
{
  "google-vertex": {
    "id": "google-vertex",
    "name": "Vertex",
    "env": ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"],
    "npm": "@ai-sdk/google-vertex",
    "models": {
      "gemini-4-argon": {
        "id": "gemini-4-argon",
        "name": "Gemini 4 Argon",
        "tool_call": true,
        "reasoning": true,
        "attachment": true,
        "temperature": true,
        "cost": {"input": 1.25, "output": 10},
        "limit": {"context": 1048576, "output": 65536}
      },
      "gemini-3.8-flash": {
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "tool_call": true,
        "reasoning": true,
        "attachment": true,
        "temperature": true,
        "cost": {"input": 0.15, "output": 0.6},
        "limit": {"context": 1048576, "output": 65536}
      }
    }
  }
}
JSON

cat << 'JSON' > "$HOME/.config/opencode/opencode.json"
{
  "$schema": "https://opencode.ai/config.json",
  "model": "google-vertex/gemini-4-argon",
  "small_model": "google-vertex/gemini-3.8-flash",
  "disabled_providers": ["google-vertex-anthropic", "opencode"]
}
JSON

cat << 'JSON' > "$HOME/.local/state/opencode/model.json"
{"recent": [{"providerID": "google-vertex", "modelID": "gemini-4-argon"}], "favorite": [], "variant": {}}
JSON

if [ -n "${GITHUB_TOKEN:-}" ]; then
  export SCION_GIT_TOKEN="$GITHUB_TOKEN"
  unset GITHUB_TOKEN
fi
exec opencode --auto -m google-vertex/gemini-4-argon "$@"
"""

HERMES_LAUNCH_SCRIPT = """#!/bin/sh
mkdir -p "$HOME/.hermes"

# Hermes env_loader.py loads ~/.hermes/.env with override=True, which overrides config.yaml.
# Ensure VERTEX_REGION is global and purge any stale regional values.
touch "$HOME/.hermes/.env"
grep -v '^VERTEX_REGION=' "$HOME/.hermes/.env" > "$HOME/.hermes/.env.tmp" 2>/dev/null || true
echo "VERTEX_REGION=global" >> "$HOME/.hermes/.env.tmp"
mv "$HOME/.hermes/.env.tmp" "$HOME/.hermes/.env"

export VERTEX_REGION=global
export GOOGLE_CLOUD_LOCATION=global
export HERMES_INFERENCE_MODEL=google/gemini-4-argon

PROJECT="${GOOGLE_CLOUD_PROJECT:-${HUB_PROJECT:-}}"

cat << YAML > "$HOME/.hermes/config.yaml"
provider: vertex
model: google/gemini-4-argon
hooks_auto_accept: true
vertex:
  project_id: ${PROJECT}
  region: global
YAML

TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prompt)
      shift
      TASK="${1:-}"
      ;;
    --continue)
      ;;
  esac
  shift 2>/dev/null || break
done

if [ -n "$TASK" ]; then
  (sleep 4 && tmux send-keys -t scion -l "$TASK" && tmux send-keys -t scion Enter) &
fi

exec hermes chat --yolo --accept-hooks --provider vertex -m google/gemini-4-argon
"""


def patch_file(path, content, make_exec=False, apply=False):
    if not os.path.exists(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    current = None
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            current = f.read()
    if current == content:
        return f"{path}: unchanged (already patched)"
    if not apply:
        return f"{path}: NOT patched"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if make_exec:
        os.chmod(path, 0o755)
    return f"{path}: patched now"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply patches")
    args = parser.parse_args()

    lines = []

    # 1. Backend / OpenCode homes
    backend_homes = glob.glob(os.path.join(SCION_HOME, "project-configs", "*", ".scion", "agents", "backend", "home"))
    for bh in backend_homes:
        launch_sh = os.path.join(bh, ".local", "bin", "opencode-launch.sh")
        lines.append(patch_file(launch_sh, OPENCODE_LAUNCH_SCRIPT, make_exec=True, apply=args.apply))

        cfg_json = os.path.join(bh, ".config", "opencode", "opencode.json")
        lines.append(patch_file(cfg_json, json.dumps(OPENCODE_CONFIG, indent=2) + "\n", apply=args.apply))

        zshrc = os.path.join(bh, ".zshrc")
        if os.path.exists(zshrc) or args.apply:
            z_cur = open(zshrc).read() if os.path.exists(zshrc) else ""
            z_additions = []
            if "GOOGLE_CLOUD_LOCATION=global" not in z_cur:
                z_additions.append("export GOOGLE_CLOUD_LOCATION=global")
            if "VERTEX_LOCATION=global" not in z_cur:
                z_additions.append("export VERTEX_LOCATION=global")
            if z_additions:
                if args.apply:
                    with open(zshrc, "a", encoding="utf-8") as f:
                        f.write("\n" + "\n".join(z_additions) + "\n")
                    lines.append(f"{zshrc}: appended location exports")
                else:
                    lines.append(f"{zshrc}: NOT patched")
            else:
                lines.append(f"{zshrc}: unchanged (exports present)")

    # 2. Reviewer / Hermes homes
    reviewer_homes = glob.glob(os.path.join(SCION_HOME, "project-configs", "*", ".scion", "agents", "reviewer", "home"))
    for rh in reviewer_homes:
        launch_sh = os.path.join(rh, ".local", "bin", "hermes-launch.sh")
        lines.append(patch_file(launch_sh, HERMES_LAUNCH_SCRIPT, make_exec=True, apply=args.apply))

        env_file = os.path.join(rh, ".hermes", ".env")
        if os.path.exists(env_file) or args.apply:
            os.makedirs(os.path.dirname(env_file), exist_ok=True)
            e_lines = []
            if os.path.exists(env_file):
                for l in open(env_file).read().splitlines():
                    if not l.startswith("VERTEX_REGION="):
                        e_lines.append(l)
            e_lines.append("VERTEX_REGION=global")
            lines.append(patch_file(env_file, "\n".join(e_lines) + "\n", apply=args.apply))

        cfg_file = os.path.join(rh, ".hermes", "config.yaml")
        if os.path.exists(cfg_file) or args.apply:
            os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
            proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("HUB_PROJECT") or os.environ.get("HUB_PROJECT_ID", "")
            hermes_cfg = f"""provider: vertex
model: google/gemini-4-argon
hooks_auto_accept: true
vertex:
  project_id: {proj}
  region: global
onboarding:
  seen:
    busy_input_prompt: true
    tool_progress_prompt: true
"""
            lines.append(patch_file(cfg_file, hermes_cfg, apply=args.apply))

    # 3. Global and hydrated harness configs
    hermes_hcs = [os.path.join(SCION_HOME, "harness-configs", "hermes", "config.yaml")] + glob.glob(
        os.path.join(SCION_HOME, "workspace-storage", "hubs", "*", "harness-configs", "*", "hermes", "config.yaml")
    )
    for hermes_hc in hermes_hcs:
        if os.path.exists(hermes_hc):
            try:
                import yaml
                with open(hermes_hc, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                changed = False
                if "command" not in data:
                    data["command"] = {}
                if data["command"].get("base") != ["sh", "/home/scion/.local/bin/hermes-launch.sh"]:
                    data["command"]["base"] = ["sh", "/home/scion/.local/bin/hermes-launch.sh"]
                    data["command"]["resume_flag"] = "--continue"
                    data["command"]["task_flag"] = "--prompt"
                    data["command"]["task_position"] = "before_base_args"
                    changed = True
                if "model_aliases" not in data:
                    data["model_aliases"] = {}
                aliases = {
                    "small": "google/gemini-3.8-flash",
                    "medium": "google/gemini-4-argon",
                    "large": "google/gemini-4-argon",
                    "extra-large": "google/gemini-4-argon",
                }
                if data["model_aliases"] != aliases:
                    data["model_aliases"] = aliases
                    changed = True
                if changed:
                    new_yaml = yaml.dump(data, sort_keys=False)
                    lines.append(patch_file(hermes_hc, new_yaml, apply=args.apply))
                else:
                    lines.append(f"{hermes_hc}: unchanged (already configured)")
            except Exception as e:
                lines.append(f"{hermes_hc}: error {e}")

    opencode_hcs = [os.path.join(SCION_HOME, "harness-configs", "opencode", "config.yaml")] + glob.glob(
        os.path.join(SCION_HOME, "workspace-storage", "hubs", "*", "harness-configs", "*", "opencode", "config.yaml")
    )
    for opencode_hc in opencode_hcs:
        if os.path.exists(opencode_hc):
            try:
                import yaml
                with open(opencode_hc, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                changed = False
                if "model_aliases" not in data:
                    data["model_aliases"] = {}
                aliases = {
                    "small": "google-vertex/gemini-3.8-flash",
                    "medium": "google-vertex/gemini-4-argon",
                    "large": "google-vertex/gemini-4-argon",
                    "extra-large": "google-vertex/gemini-4-argon",
                }
                if data["model_aliases"] != aliases:
                    data["model_aliases"] = aliases
                    changed = True
                if changed:
                    new_yaml = yaml.dump(data, sort_keys=False)
                    lines.append(patch_file(opencode_hc, new_yaml, apply=args.apply))
                else:
                    lines.append(f"{opencode_hc}: unchanged (already configured)")
            except Exception as e:
                lines.append(f"{opencode_hc}: error {e}")

    # 4. Hub DB harness_configs table
    hub_db = os.path.join(SCION_HOME, "hub.db")
    if os.path.exists(hub_db) and args.apply:
        try:
            import sqlite3
            conn = sqlite3.connect(hub_db)
            cur = conn.cursor()
            # update hermes
            cur.execute("SELECT config FROM harness_configs WHERE name = 'hermes'")
            row = cur.fetchone()
            if row:
                cfg = json.loads(row[0])
                cfg["command"] = {
                    "base": ["sh", "/home/scion/.local/bin/hermes-launch.sh"],
                    "resume_flag": "--continue",
                    "task_flag": "--prompt",
                    "task_position": "before_base_args"
                }
                cfg["modelAliases"] = {
                    "small": "google/gemini-3.8-flash",
                    "medium": "google/gemini-4-argon",
                    "large": "google/gemini-4-argon",
                    "extra-large": "google/gemini-4-argon"
                }
                cur.execute("UPDATE harness_configs SET config = ? WHERE name = 'hermes'", (json.dumps(cfg),))
            # update opencode
            cur.execute("SELECT config FROM harness_configs WHERE name = 'opencode'")
            row = cur.fetchone()
            if row:
                cfg = json.loads(row[0])
                cfg["modelAliases"] = {
                    "small": "google-vertex/gemini-3.8-flash",
                    "medium": "google-vertex/gemini-4-argon",
                    "large": "google-vertex/gemini-4-argon",
                    "extra-large": "google-vertex/gemini-4-argon"
                }
                cur.execute("UPDATE harness_configs SET config = ? WHERE name = 'opencode'", (json.dumps(cfg),))
            conn.commit()
            conn.close()
            lines.append(f"{hub_db}: updated harness_configs for hermes and opencode")
        except Exception as e:
            lines.append(f"{hub_db}: error updating sqlite {e}")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
