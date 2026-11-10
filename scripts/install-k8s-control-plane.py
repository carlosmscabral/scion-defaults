import argparse
import base64
import json
import os
import subprocess
import sys

os.environ.setdefault("CLOUDSDK_CORE_DISABLE_PROMPTS", "1")


def render_manifests(
    namespace: str,
    hub_url: str,
    factory_projects: str,
    sandbox_project: str,
    firestore_project: str,
    firestore_database: str = "(default)",
    ksa_name: str = "scion-factory-control",
    gsa_email: str = "",
    image: str = "python:3.12-slim",
    iap_audience: str = "",
    enable_delta: bool = False,
    github_repo: str = "",
    issue_label: str = "",
    lead_slug: str = "",
    build_branch: str = "",
    runtime_profile: str = "",
) -> list[dict]:

    script_dir = os.path.dirname(os.path.abspath(__file__))
    scripts_to_include = [
        "hub_client.py",
        "factory_store.py",
        "sync-factory-agents.py",
        "factory-watchdog.py",
        "factory_delta.py",
    ]

    configmap_data = {}
    for script in scripts_to_include:
        path = os.path.join(script_dir, script)
        if os.path.exists(path):
            with open(path, "r") as f:
                configmap_data[script] = f.read()
        else:
            configmap_data[script] = "# not found during render"

    manifests = []

    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": ksa_name,
            "namespace": namespace,
            "annotations": {},
        },
    }
    if gsa_email:
        sa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] = gsa_email
    manifests.append(sa)

    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "scion-factory-control-scripts",
            "namespace": namespace,
        },
        "data": configmap_data,
    }
    manifests.append(cm)

    def make_cronjob(name, schedule, command, extra_env=None):
        env = [
            {"name": "HUB_URL", "value": hub_url},
            {"name": "FACTORY_PROJECTS", "value": factory_projects},
            {"name": "SANDBOX_PROJECT", "value": sandbox_project},
            {"name": "FIRESTORE_PROJECT", "value": firestore_project},
            {"name": "FIRESTORE_DATABASE", "value": firestore_database},
            {"name": "AGENT_RUNTIME", "value": "kubernetes"},
            {
                "name": "SCION_HUB_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": "scion-factory-hub-auth", "key": "token", "optional": True}},
            },
        ]
        if iap_audience:
            env.append({"name": "HUB_IAP_AUDIENCE", "value": iap_audience})
        if extra_env:
            env.extend(extra_env)

        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": name,
                "namespace": namespace,
            },
            "spec": {
                "schedule": schedule,
                "concurrencyPolicy": "Forbid",
                "jobTemplate": {
                    "spec": {
                        "backoffLimit": 1,
                        "template": {
                            "spec": {
                                "serviceAccountName": ksa_name,
                                "containers": [
                                    {
                                        "name": "worker",
                                        "image": image,
                                        "command": command,
                                        "env": env,
                                        "volumeMounts": [
                                            {
                                                "name": "scripts",
                                                "mountPath": "/opt/scion-factory",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [
                                    {
                                        "name": "scripts",
                                        "configMap": {
                                            "name": "scion-factory-control-scripts",
                                        },
                                    }
                                ],
                                "restartPolicy": "Never",
                            }
                        },
                    }
                },
            },
        }

    sync_cj = make_cronjob(
        "scion-factory-sync",
        "*/1 * * * *",
        ["python3", "/opt/scion-factory/sync-factory-agents.py"],
    )
    manifests.append(sync_cj)

    watchdog_cj = make_cronjob(
        "scion-factory-watchdog",
        "*/5 * * * *",
        ["python3", "/opt/scion-factory/factory-watchdog.py", "--apply"],
        extra_env=[{"name": "WATCHDOG_STATE_BACKEND", "value": "firestore"}],
    )
    manifests.append(watchdog_cj)

    if enable_delta:
        delta_cj = make_cronjob(
            "scion-factory-delta",
            "*/2 * * * *",
            ["python3", "/opt/scion-factory/factory_delta.py", "--apply"],
            extra_env=[
                {"name": "GITHUB_REPO", "value": github_repo},
                {"name": "ISSUE_LABEL", "value": issue_label},
                {"name": "LEAD_SLUG", "value": lead_slug},
                {"name": "BUILD_BRANCH", "value": build_branch},
                {"name": "RUNTIME_PROFILE", "value": runtime_profile},
                {
                    "name": "GITHUB_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "scion-factory-github-auth",
                            "key": "token",
                            "optional": True,
                        }
                    },
                },
            ],
        )
        manifests.append(delta_cj)

    return manifests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--hub-url", default="")
    parser.add_argument("--factory-projects", default="")
    parser.add_argument("--sandbox-project", default="")
    parser.add_argument("--firestore-project", default="")
    parser.add_argument("--gsa-email", default="")
    parser.add_argument("--iap-audience", default="")
    parser.add_argument("--enable-delta", action="store_true")
    parser.add_argument("--github-repo", default="")
    parser.add_argument("--issue-label", default="")
    parser.add_argument("--lead-slug", default="")
    parser.add_argument("--build-branch", default="")
    parser.add_argument("--runtime-profile", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    enable_delta = bool(
        args.enable_delta
        or os.environ.get("ENABLE_DELTA", "").strip().lower() in ("1", "true", "yes")
    )
    manifests = render_manifests(
        namespace=args.namespace,
        hub_url=args.hub_url or os.environ.get("HUB_URL", ""),
        factory_projects=args.factory_projects or os.environ.get("FACTORY_PROJECTS", ""),
        sandbox_project=args.sandbox_project or os.environ.get("SANDBOX_PROJECT", ""),
        firestore_project=args.firestore_project or os.environ.get("FIRESTORE_PROJECT", ""),
        gsa_email=args.gsa_email or os.environ.get("GSA_EMAIL", ""),
        iap_audience=args.iap_audience or os.environ.get("HUB_IAP_AUDIENCE", ""),
        enable_delta=enable_delta,
        github_repo=args.github_repo or os.environ.get("GITHUB_REPO", ""),
        issue_label=args.issue_label or os.environ.get("ISSUE_LABEL", ""),
        lead_slug=args.lead_slug or os.environ.get("LEAD_SLUG", ""),
        build_branch=args.build_branch or os.environ.get("BUILD_BRANCH", ""),
        runtime_profile=args.runtime_profile or os.environ.get("RUNTIME_PROFILE", ""),
    )

    json_docs = "\n---\n".join(json.dumps(m) for m in manifests)
    if args.out:
        with open(args.out, "w") as f:
            f.write(json_docs)

    env_dry = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    is_dry_run = args.dry_run or not args.apply or env_dry

    if is_dry_run:
        print(json_docs)
    else:
        token = os.environ.get("SCION_HUB_TOKEN", "").strip()
        if token:
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "scion-factory-hub-auth",
                    "namespace": args.namespace,
                },
                "type": "Opaque",
                "data": {
                    "token": base64.b64encode(token.encode("utf-8")).decode("ascii"),
                },
            }
            subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(secret), text=True, check=True)

        gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if gh_token:
            gh_secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "scion-factory-github-auth",
                    "namespace": args.namespace,
                },
                "type": "Opaque",
                "data": {
                    "token": base64.b64encode(gh_token.encode("utf-8")).decode("ascii"),
                },
            }
            subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(gh_secret), text=True, check=True)

        subprocess.run(["kubectl", "apply", "-f", "-"], input=json_docs, text=True, check=True)


if __name__ == "__main__":
    main()
