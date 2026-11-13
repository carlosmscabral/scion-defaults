#!/usr/bin/env python3
"""Render compiled agent templates (`--src <kit>/templates`) with your factory.env values. Runs on YOUR machine.

    scripts/render-templates.py --src /tmp/kit/templates      # -> build/templates/<role>/ (git-ignored by build/)
    scripts/render-templates.py --src /tmp/kit/templates --out /tmp/tpl --roles lead,backend
    scripts/render-templates.py --file config/heartbeat-message.txt   # render one file to stdout
    scripts/render-templates.py --src /tmp/kit/templates --agent-names # "<role>\t<agent name>" per role, for `scion start`

For every role folder in templates/ (folders starting with "_" are shared parts, not roles):
  * copies all files, replacing ${VAR} for the variables in factory.env plus the derived DEPLOYER_SA and
    RUNTIME_SA. Nothing else is touched: "$schema", "$HOME", "$$" and "${GITHUB_TOKEN:-}" stay as they are, so
    shell scripts and JSON files survive. (Python's string.Template would turn "$$" into "$".)
  * also replaces ${<ROLE>_AGENT} (LEAD_AGENT, EVAL_LEAD_AGENT, ...) with that role's agent name:
    "<AGENT_PREFIX>-<role>" when factory.env sets AGENT_PREFIX (several factories on one Hub, e.g. one per
    monorepo product), else the bare role. ${FACTORY_SUBDIR} is the product folder relative to the repository
    root ("." when unset). See docs/plans/2026-10-22-monorepo-factories.md.
  * writes agents.md (the template's agent_instructions file) from templates/_shared/agents.md, the operating
    protocol every agent follows, followed by the role's own agents.extra.md when it has one.
It fails if a referenced variable has no value, so a half-filled factory.env can't produce broken prompts.

Upload the result as described in templates/README.md.
"""
import argparse
import importlib.util
import os
import re
import shutil
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _sibling(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ${CLAUDE_HARNESS} & co.: the <harness>-factory harness-configs when the factory has a product folder.
harness_vars = _sibling("make_factory_harness_configs", "make-factory-harness-configs.py").harness_vars
VARS = ("HUB_PROJECT HUB_ZONE HUB_VM HUB_OS_USER HUB_LOCAL_URL SANDBOX_PROJECT SANDBOX_REGION FIRESTORE_PROJECT "
        "FIRESTORE_DATABASE SPANNER_INSTANCE SPANNER_DATABASE BQ_DATASET DATA_BUCKET DEPLOYER_SA_NAME RUNTIME_SA_NAME SERVING_SERVICE DASHBOARD_SERVICE "
        "GITHUB_REPO BUILD_PROJECT EVAL_PROJECT BUILD_BRANCH EVAL_BRANCH PO_EMAIL DEPLOYER_SA RUNTIME_SA "
        "AGENT_PREFIX FACTORY_SUBDIR KNOWLEDGE_PLANE").split()
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_AGENT_NAME = 63  # Scion slugifies agent names and caps them at 63 characters (pkg/api/slug.go).


def agent_name(prefix, role):
    """The Hub agent name of `role`: "<prefix>-<role>", or the bare role without a prefix."""
    return f"{prefix}-{role}" if prefix else role


def role_agent_vars(prefix, roles):
    """One ${<ROLE>_AGENT} variable per role, e.g. EVAL_LEAD_AGENT -> "project1-eval-lead"."""
    return {role.upper().replace("-", "_") + "_AGENT": agent_name(prefix, role) for role in roles}


def validate_prefix(prefix, roles):
    """Fail unless every prefixed name is a valid Scion agent name exactly as written (no silent slugify)."""
    if not prefix:
        return
    if not SLUG.match(prefix):
        raise ValueError(f"AGENT_PREFIX {prefix!r} must be lowercase letters and digits separated by single dashes "
                         "(the factory's project slug, e.g. project1)")
    too_long = [agent_name(prefix, r) for r in roles if len(agent_name(prefix, r)) > MAX_AGENT_NAME]
    if too_long:
        raise ValueError(f"AGENT_PREFIX {prefix!r} makes agent names longer than {MAX_AGENT_NAME} characters: "
                         + ", ".join(too_long))


def normalize_subdir(value):
    """FACTORY_SUBDIR as a clean path relative to the repository root ("." for the root itself)."""
    value = (value or "").strip()
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if value.startswith("/") or ".." in parts:
        raise ValueError(f"FACTORY_SUBDIR {value!r} must be a folder inside the repository, relative to its root "
                         "(e.g. src/project1)")
    return "/".join(parts) or "."


def knowledge_plane_path(subdir):
    """${KNOWLEDGE_PLANE}: the factory's knowledge plane in the checkout (each monorepo product keeps its own)."""
    return "/workspace/knowledge-plane" if subdir == "." else f"/workspace/{subdir}/knowledge-plane"


def load_env(path):
    values = {}
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for name in ("DEPLOYER", "RUNTIME"):
        if values.get(f"{name}_SA_NAME") and values.get("SANDBOX_PROJECT"):
            values.setdefault(f"{name}_SA", f"{values[name + '_SA_NAME']}@{values['SANDBOX_PROJECT']}"
                                            ".iam.gserviceaccount.com")
    return {k: v for k, v in values.items() if k in VARS}


def render(text, values, missing, where):
    def repl(match):
        name = match.group(1)
        # ${<ROLE>_AGENT} / ${<HARNESS>_HARNESS} always count as ours, so a misspelled role (${LEED_AGENT}) fails
        # instead of leaking into a prompt.
        if name not in VARS and not name.endswith(("_AGENT", "_HARNESS")):
            return match.group(0)
        if not values.get(name):
            missing.add(f"{name} (in {where})")
            return match.group(0)
        return values[name]
    return PLACEHOLDER.sub(repl, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", default=os.environ.get("FACTORY_ENV", os.path.join(ROOT, "config", "factory.env")))
    parser.add_argument("--src", default="",
                        help="compiled templates directory (e.g. <kit>/templates from `scion-factory compile`)")
    parser.add_argument("--out", default=os.path.join(ROOT, "build", "templates"))
    parser.add_argument("--roles", help="comma-separated subset of roles (default: all)")
    parser.add_argument("--file", help="render this one file to stdout instead of the templates")
    parser.add_argument("--agent-names", action="store_true",
                        help='print "<role>\\t<agent name>" for every role and exit')
    args = parser.parse_args()

    if not args.src and not args.file:
        parser.error("--src is required (pass --src <kit>/templates generated by `scion-factory compile`)")
    if not os.path.exists(args.env):
        sys.exit(f"missing {args.env} (copy config/factory.env.example and fill it in)")
    values = load_env(args.env)
    src_dir_root = args.src
    if not src_dir_root and args.file:
        candidate_tpl = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(args.file))), "templates")
        if os.path.isdir(candidate_tpl):
            src_dir_root = candidate_tpl
    if src_dir_root:
        if not os.path.isdir(src_dir_root):
            sys.exit(f"missing templates directory: {src_dir_root}")
        all_roles = sorted(d for d in os.listdir(src_dir_root)
                           if os.path.isdir(os.path.join(src_dir_root, d)) and not d.startswith("_"))
    else:
        all_roles = []
    prefix = values.get("AGENT_PREFIX", "")
    try:
        validate_prefix(prefix, all_roles)
        values["FACTORY_SUBDIR"] = normalize_subdir(values.get("FACTORY_SUBDIR", ""))
    except ValueError as e:
        sys.exit(str(e))
    values.update(role_agent_vars(prefix, all_roles))
    values.update(harness_vars(values["FACTORY_SUBDIR"], values.get("AGENT_RUNTIME", "docker")))
    values["KNOWLEDGE_PLANE"] = knowledge_plane_path(values["FACTORY_SUBDIR"])
    if args.agent_names:
        for role in all_roles:
            print(f"{role}\t{agent_name(prefix, role)}")
        return 0
    if args.file:
        missing = set()
        text = render(open(args.file).read(), values, missing, args.file)
        if missing:
            sys.exit("missing values in factory.env:\n  " + "\n  ".join(sorted(missing)))
        sys.stdout.write(text.rstrip("\n"))
        return 0
    roles = all_roles
    if args.roles:
        wanted = args.roles.split(",")
        unknown = set(wanted) - set(roles)
        if unknown:
            sys.exit(f"unknown roles: {', '.join(sorted(unknown))}")
        roles = wanted
    shared_path = os.path.join(src_dir_root, "_shared", "agents.md")
    shared = open(shared_path).read()

    missing = set()
    for role in roles:
        src_dir, out_dir = os.path.join(src_dir_root, role), os.path.join(args.out, role)
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        for dirpath, _, files in os.walk(src_dir):
            for name in files:
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, src_dir)
                if rel == "agents.extra.md":
                    continue
                dst = os.path.join(out_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src) as f:
                    text = render(f.read(), values, missing, f"{role}/{rel}")
                with open(dst, "w") as f:
                    f.write(text)
                shutil.copymode(src, dst)
        extra_path = os.path.join(src_dir, "agents.extra.md")
        agents = render(shared, values, missing, "_shared/agents.md")
        if os.path.exists(extra_path):
            agents += "\n" + render(open(extra_path).read(), values, missing, f"{role}/agents.extra.md")
        with open(os.path.join(out_dir, "agents.md"), "w") as f:
            f.write(agents)
        for script in ("home/.local/bin/opencode-launch.sh", "home/.local/bin/hermes-launch.sh"):
            path = os.path.join(out_dir, script)
            if os.path.exists(path):
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"rendered {role} -> {out_dir}")

    if missing:
        sys.exit("missing values in factory.env:\n  " + "\n  ".join(sorted(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
