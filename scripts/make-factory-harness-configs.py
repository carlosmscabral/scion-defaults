#!/usr/bin/env python3
"""Build and publish the <harness>-factory harness-configs that start agents in their monorepo product folder.

Runs on the Hub VM as the Hub OS user (it needs the `scion` CLI logged in as a Hub admin for --global sync). From
your machine, --via-hub ships it there through run-on-hub.sh with factory-cd.sh embedded:

    scripts/make-factory-harness-configs.py --via-hub              # dry run on the Hub: print the plan
    scripts/make-factory-harness-configs.py --via-hub --apply      # build and publish

For each harness (claude, antigravity, opencode, hermes) it copies ~/.scion/harness-configs/<harness> to
~/.scion/harness-configs/<harness>-factory (a fresh copy every run, so provisioner fixes from a Scion upgrade reach
it), installs config/harness/home/.local/bin/factory-cd.sh into the copy's home/.local/bin/, puts
["sh", "/home/scion/.local/bin/factory-cd.sh"] in front of its command.base, and publishes it with
`scion harness-config sync <harness>-factory --global`.

The bundled configs are never edited: Scion refreshes them from its binary on every Hub restart. factory-cd.sh does
nothing unless FACTORY_SUBDIR is set, so the -factory configs are safe for any template. Templates rendered with a
FACTORY_SUBDIR select them through ${CLAUDE_HARNESS} & co. (scripts/render-templates.py).
See docs/plans/2026-10-22-monorepo-factories.md.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER_SRC = os.path.join(os.path.dirname(HERE), "config", "harness", "home", ".local", "bin", "factory-cd.sh")
EMBEDDED_LAUNCHER = None  # base64 of factory-cd.sh; --via-hub fills it in, because run-on-hub.sh ships one file only
LAUNCHER = ["sh", "/home/scion/.local/bin/factory-cd.sh"]
HARNESSES = ("claude", "antigravity", "opencode", "hermes")
SUFFIX = "-factory"
BASE_LINE = re.compile(r"^(?P<indent>[ \t]+)base:[ \t]*(?P<list>\[.*\])[ \t]*$", re.M)
DEFAULT_PROVISIONER_TIMEOUT = "60s"
DEFAULT_AGY_VERSION_TIMEOUT_SEC = 25


def harness_vars(subdir, runtime="docker"):
    """${<HARNESS>_HARNESS} for the templates: the -factory config for monorepo subdirs or GKE pods."""
    suffix = SUFFIX if (subdir not in ("", ".") or runtime == "kubernetes") else ""
    return {f"{h.upper()}_HARNESS": h + suffix for h in HARNESSES}


def harden_config_yaml(config_text, harness, auth_default_type=None, provisioner_timeout=DEFAULT_PROVISIONER_TIMEOUT):
    """Apply Vertex AI auth default and Autopilot cold-start provisioner timeout cleanly when configured."""
    if harness == "antigravity" and auth_default_type is None:
        auth_default_type = "vertex-ai"
    text = config_text
    if auth_default_type:
        if re.search(r"^auth:\s*$", text, re.M):
            if re.search(r"^[ \t]+default_type:", text, re.M):
                text = re.sub(
                    r"(^auth:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+default_type:[ \t]*)[^\n]+",
                    rf"\g<1>{auth_default_type}",
                    text,
                    count=1,
                    flags=re.M,
                )
            else:
                text = re.sub(r"(^auth:\s*\n)", rf"\g<1>  default_type: {auth_default_type}\n", text, count=1, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n\nauth:\n  default_type: {auth_default_type}\n"
    if harness == "antigravity" and provisioner_timeout:
        if re.search(r"^provisioner:\s*$", text, re.M):
            if re.search(r"^provisioner:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+timeout:", text, re.M):
                text = re.sub(
                    r"(^provisioner:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+timeout:[ \t]*)[^\n]+",
                    rf"\g<1>{provisioner_timeout}",
                    text,
                    count=1,
                    flags=re.M,
                )
            else:
                text = re.sub(r"(^provisioner:\s*\n)", rf"\g<1>  timeout: {provisioner_timeout}\n", text, count=1, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n\nprovisioner:\n  timeout: {provisioner_timeout}\n"
    return text


def harden_provision_py(py_text, version_timeout=DEFAULT_AGY_VERSION_TIMEOUT_SEC):
    """Increase cold-start `agy --version` subprocess timeout in provision.py so Autopilot FUSE mounts don't flake."""
    if "agy" not in py_text or "_get_agy_version" not in py_text:
        return py_text
    return re.sub(
        r'(timeout\s*=\s*)([1-9]|1[0-9]|2[0-4])\b',
        rf"\g<1>{int(version_timeout)}",
        py_text,
    )


def prefix_command_base(config_text):
    """Put the factory-cd launcher in front of command.base (a JSON-style flow list), once."""
    match = None
    in_command = False
    for line_match in re.finditer(r"^.*$", config_text, re.M):
        line = line_match.group(0)
        if re.match(r"^command:\s*$", line):
            in_command = True
            continue
        if in_command and line and not line[0].isspace() and not line.startswith("#"):
            in_command = False
        if in_command:
            m = BASE_LINE.match(line)
            if m:
                match = (line_match.start(), line_match.end(), m)
                break
    if match is None:
        raise ValueError("config.yaml has no command.base flow list (base: [\"...\"]) to wrap")
    start, end, m = match
    base = json.loads(m.group("list"))
    if base[:len(LAUNCHER)] == LAUNCHER:
        return config_text, False
    new_line = f"{m.group('indent')}base: {json.dumps(LAUNCHER + base)}"
    return config_text[:start] + new_line + config_text[end:], True


def build(scion_home, harness, apply):
    src = os.path.join(scion_home, "harness-configs", harness)
    dst = os.path.join(scion_home, "harness-configs", harness + SUFFIX)
    config_path = os.path.join(src, "config.yaml")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"no bundled harness-config {harness} at {src}")
    with open(config_path) as f:
        text, _ = prefix_command_base(f.read())
    text = harden_config_yaml(text, harness)
    print(f"{harness} -> {harness + SUFFIX}: copy {src} to {dst}, command.base = {json.dumps(LAUNCHER)} + bundled base")
    if not apply:
        return dst
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False,
                    ignore=shutil.ignore_patterns("*.bak.*", ".*.tmp-*", "__pycache__"))
    with open(os.path.join(dst, "config.yaml"), "w") as f:
        f.write(text)
    prov_path = os.path.join(dst, "provision.py")
    if os.path.isfile(prov_path):
        with open(prov_path) as f:
            prov_orig = f.read()
        prov_hardened = harden_provision_py(prov_orig)
        if prov_hardened != prov_orig:
            with open(prov_path, "w") as f:
                f.write(prov_hardened)
    bin_dir = os.path.join(dst, "home", ".local", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    launcher = os.path.join(bin_dir, "factory-cd.sh")
    with open(launcher, "wb") as f:
        f.write(launcher_bytes())
    os.chmod(launcher, 0o755)
    return dst


def launcher_bytes():
    if EMBEDDED_LAUNCHER:
        return base64.b64decode(EMBEDDED_LAUNCHER)
    with open(LAUNCHER_SRC, "rb") as f:
        return f.read()


def via_hub(argv):
    """Run this script on the Hub VM with factory-cd.sh embedded (run-on-hub.sh copies only the script)."""
    with open(os.path.abspath(__file__)) as f:
        source = f.read()
    embedded = base64.b64encode(launcher_bytes()).decode()
    source = source.replace("EMBEDDED_LAUNCHER = None", f"EMBEDDED_LAUNCHER = {embedded!r}", 1)
    runner = os.environ.get("RUN_ON_HUB", os.path.join(HERE, "run-on-hub.sh"))
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "make_factory_harness_configs_remote.py")
        with open(script, "w") as f:
            f.write(source)
        return subprocess.run([runner, script, *[a for a in argv if a != "--via-hub"]]).returncode



def via_api(client, harnesses, apply):
    import hashlib
    import urllib.request
    status, data = client.request("GET", "/api/v1/harness-configs?scope=global")
    hc_list = (
        data.get("harnessConfigs")
        if isinstance(data, dict) and isinstance(data.get("harnessConfigs"), list)
        else (data.get("items", []) if isinstance(data, dict) else [])
    )
    configs = {c["name"]: c for c in hc_list}
    
    for harness in harnesses:
        if harness not in configs:
            print(f"ERROR: {harness} not found globally on Hub", file=sys.stderr)
            continue
        c = configs[harness]
        status, urls_data = client.request("GET", f"/api/v1/harness-configs/{c['id']}/download")
        urls = urls_data.get("files") or urls_data.get("urls") or urls_data.get("downloadUrls") or []
        
        files = {}
        config_text = ""
        for u in urls:
            req = urllib.request.Request(u["url"])
            same_origin = client._is_same_origin(u["url"]) if hasattr(client, "_is_same_origin") else False
            if same_origin:
                if client.transport_mode == "iap" and getattr(client, "transport_token", None):
                    req.add_header("Proxy-Authorization", "Bearer " + client.transport_token)
                elif client.transport_mode == "serverless" and getattr(client, "transport_token", None):
                    req.add_header("X-Serverless-Authorization", "Bearer " + client.transport_token)
                if getattr(client, "token", None):
                    req.add_header("Authorization", "Bearer " + client.token)
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()
                files[u["path"]] = content
                if u["path"] == "config.yaml":
                    config_text = content.decode("utf-8")
        
        if not config_text:
            print(f"ERROR: config.yaml not found in {harness}", file=sys.stderr)
            continue
            
        new_text, changed = prefix_command_base(config_text)
        new_text = harden_config_yaml(new_text, harness)
        files["config.yaml"] = new_text.encode("utf-8")
        if "provision.py" in files:
            files["provision.py"] = harden_provision_py(files["provision.py"].decode("utf-8", errors="replace")).encode("utf-8")
        files["home/.local/bin/factory-cd.sh"] = launcher_bytes()
        
        print(f"{harness} -> {harness + SUFFIX}: download from API, command.base = {json.dumps(LAUNCHER)} + bundled base")
        
        if apply:
            payload = {"name": harness + SUFFIX, "harness": harness, "scope": "global"}
            if harness + SUFFIX in configs:
                hc_id = configs[harness + SUFFIX]["id"]
            else:
                status, res = client.request("POST", "/api/v1/harness-configs", body=payload)
                hc_obj = res.get("harnessConfig") if isinstance(res, dict) and isinstance(res.get("harnessConfig"), dict) else res
                hc_id = hc_obj["id"]
            
            upload_payload = {"files": [{"path": p, "size": len(c)} for p, c in files.items()]}
            status, up_res = client.request("POST", f"/api/v1/harness-configs/{hc_id}/upload", body=upload_payload)
            
            up_urls = up_res.get("uploadUrls") or up_res.get("urls") or up_res.get("files") or []
            for u in up_urls:
                client.request(u.get("method", "PUT"), u["url"], raw=files[u["path"]], headers=u.get("headers") or {"Content-Type": "application/octet-stream"})
                
            manifest = {
                "version": "1.0",
                "harness": harness,
                "files": [
                    {
                        "path": p,
                        "size": len(c),
                        "hash": "sha256:" + hashlib.sha256(c).hexdigest(),
                        "mode": "0644" if p == "config.yaml" else "0755",
                    }
                    for p, c in files.items()
                ],
            }
            fin_body = {"manifest": manifest}
            if up_res.get("uploadId"):
                fin_body["uploadId"] = up_res["uploadId"]
            client.request("POST", f"/api/v1/harness-configs/{hc_id}/finalize", body=fin_body)
            print(f"{harness + SUFFIX} published via API")

def cli_env(scion_home, email, base=None):

    """Environment for `scion harness-config sync --global` on the Hub VM.

    Keeps a SCION_HUB_TOKEN that is already set. Otherwise mints a 10-minute admin JWT for `email` the way
    scion-admin.py does (key derived from SCION_SERVER_SESSION_SECRET in ~/.scion/hub.env), which the scion CLI
    accepts as SCION_HUB_TOKEN. The token is never printed.
    """
    import base64 as b64mod
    import hashlib
    import hmac
    import sqlite3
    import time

    env = dict(os.environ if base is None else base)
    env.setdefault("SCION_HUB_ENDPOINT", os.environ.get("HUB_LOCAL_URL", "http://localhost:8080").rstrip("/"))
    if env.get("SCION_HUB_TOKEN"):
        return env
    conn = sqlite3.connect(f"file:{os.path.join(scion_home, 'hub.db')}?mode=ro", uri=True)
    user = conn.execute("SELECT id, email, role FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if not user:
        sys.exit(f"no Hub user with email {email} (sign in to the Hub web UI once, or set PO_EMAIL)")
    with open(os.path.join(scion_home, "hub.env")) as f:
        secret = next((l.split("=", 1)[1].strip() for l in f if l.startswith("SCION_SERVER_SESSION_SECRET=")), None)
    if not secret:
        sys.exit("SCION_SERVER_SESSION_SECRET not found in hub.env")
    key = hashlib.sha256(("scion-hub-signing-key:user_signing_key:" + secret).encode()).digest()

    def b64(data):
        return b64mod.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    claims = {"iss": "scion-hub", "sub": user[0], "aud": ["scion-hub-api"], "exp": now + 600, "nbf": now - 10,
              "iat": now, "uid": user[0], "email": user[1], "name": user[1], "role": user[2], "type": "access",
              "client": "web"}
    signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + b64(json.dumps(claims).encode())
    env["SCION_HUB_TOKEN"] = signing_input + "." + b64(hmac.new(key, signing_input.encode(), hashlib.sha256).digest())
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scion-home", default=os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion")))
    parser.add_argument("--harness", default=",".join(HARNESSES), help="comma-separated harnesses")
    parser.add_argument("--apply", action="store_true", help="write the copies (default: print the plan)")
    parser.add_argument("--no-sync", action="store_true", help="don't publish with `scion harness-config sync`")

    parser.add_argument("--via-hub", action="store_true", help="run on the Hub VM through run-on-hub.sh")
    parser.add_argument("--via-api", action="store_true", help="build and publish via Hub API directly")
    args = parser.parse_args(argv)
    if args.via_hub:
        return via_hub(sys.argv[1:] if argv is None else argv)
        
    use_api = args.via_api
    if not use_api and os.environ.get("HUB_URL", "").strip():
        for h in [h for h in args.harness.split(",") if h]:
            if not os.path.exists(os.path.join(args.scion_home, "harness-configs", h, "config.yaml")):
                use_api = True
                break
                
    if use_api:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from hub_client import HubClient
        client = HubClient.from_env()
        via_api(client, [h for h in args.harness.split(",") if h], args.apply)
        return 0


    failed = []
    for harness in [h for h in args.harness.split(",") if h]:
        try:
            build(args.scion_home, harness, args.apply)
        except (OSError, ValueError) as e:
            print(f"ERROR {harness}: {e}", file=sys.stderr)
            failed.append(harness)
            continue
        if args.apply and not args.no_sync:
            cmd = ["scion", "harness-config", "sync", harness + SUFFIX, "--global"]
            print("+ " + " ".join(cmd))
            env = cli_env(args.scion_home, os.environ.get("PO_EMAIL", ""))
            if subprocess.run(cmd, env=env).returncode != 0:
                failed.append(harness)
    if not args.apply:
        print("dry run: nothing written (add --apply)")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
