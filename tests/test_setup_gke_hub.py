import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def make_fake_scion_upstream(tmp_path):
    scion_repo = tmp_path / "scion-upstream"
    (scion_repo / "image-build" / "scripts").mkdir(parents=True)
    (scion_repo / "image-build" / "gcloudignore-omni").write_text(
        "/docs/\n/scripts/\nweb/node_modules/\n*_test.go\n"
    )
    (scion_repo / "harnesses" / "claude").mkdir(parents=True)
    (scion_repo / "harnesses" / "claude" / "Dockerfile").write_text("FROM scratch\n")
    (scion_repo / "harnesses" / "antigravity").mkdir(parents=True)
    (scion_repo / "harnesses" / "antigravity" / "Dockerfile").write_text("FROM scratch\n")
    (scion_repo / "harnesses" / "gemini-cli").mkdir(parents=True)
    (scion_repo / "harnesses" / "gemini-cli" / "Dockerfile").write_text("FROM scratch\n")
    (scion_repo / "scripts" / "cloudrun").mkdir(parents=True)
    (scion_repo / "scripts" / "cloudrun" / "Dockerfile").write_text("FROM scratch\n")
    (scion_repo / "deploy" / "terraform" / "configurations" / "shared-infra").mkdir(parents=True)
    (scion_repo / "deploy" / "terraform" / "configurations" / "hub").mkdir(parents=True)
    (scion_repo / "deploy" / "terraform" / "configurations" / "hub-gke").mkdir(parents=True)
    (scion_repo / "scripts" / "single-node-vm").mkdir(parents=True)
    return scion_repo


def test_setup_gke_hub_dry_run_follows_upstream_terraform_ha_cloudrun_contract(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work"
    env = {
        **os.environ,
        "DRY_RUN": "1",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "prod",
        "PO_EMAIL": "admin@example.com",
        "EXTRA_ADMIN_EMAILS": "tfha-operator@demo-hub-proj.iam.gserviceaccount.com",
        "EXTRA_IAP_MEMBERS": "serviceAccount:tfha-operator@demo-hub-proj.iam.gserviceaccount.com",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261027",
            "--harnesses",
            "claude,antigravity,gemini",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out

    # 1. Order: state bucket -> shared-infra -> Cloud Build -> hub
    idx_shared = out.index("configurations/shared-infra")
    idx_build = out.index("build-images.sh")
    idx_hub = out.index("configurations/hub ")
    assert idx_shared < idx_build < idx_hub, out

    # 2. build-images.sh uses --target (no positional args) and maps gemini -> scion-gemini-cli
    assert "--builder cloud-build --target core-base" in out
    assert "--builder cloud-build --target scion-base" in out
    assert "--builder cloud-build --target scion-claude" in out
    assert "--builder cloud-build --target scion-antigravity" in out
    assert "--builder cloud-build --target scion-gemini-cli" in out
    assert "--push core-base" not in out

    # 3. Cloud Run hub image ignore file preserves scripts/cloudrun/entrypoint.sh and web/, while excluding /deploy/ and .terraform/
    cloudrun_ignore = (work_dir / "gcloudignore-cloudrun").read_text()
    assert "/scripts/" not in cloudrun_ignore
    assert "/deploy/" in cloudrun_ignore
    assert ".terraform/" in cloudrun_ignore

    # 4. Upstream tfvars schema (no undeclared po_email / image_tag; hub_name prefixed with tfha-)
    shared_tfvars = (work_dir / "shared-infra.tfvars").read_text()
    assert 'project_id            = "demo-hub-proj"' in shared_tfvars or 'project_id = "demo-hub-proj"' in shared_tfvars
    assert 'zone' in shared_tfvars and '"us-east1-b"' in shared_tfvars
    assert 'name_prefix' in shared_tfvars and '"tfha"' in shared_tfvars

    hub_tfvars = (work_dir / "hub.tfvars").read_text()
    assert 'hub_name' in hub_tfvars and '"tfha-prod"' in hub_tfvars
    assert 'state_prefix' in hub_tfvars and '"tfha/hubs/tfha-prod"' in hub_tfvars
    assert 'hub_image' in hub_tfvars and '"us-east1-docker.pkg.dev/demo-hub-proj/tfha-scion/hub:v20261027"' in hub_tfvars
    assert "admin@example.com" in hub_tfvars
    assert "tfha-operator@demo-hub-proj.iam.gserviceaccount.com" in hub_tfvars
    assert "serviceAccount:tfha-operator@demo-hub-proj.iam.gserviceaccount.com" in hub_tfvars
    assert "po_email" not in hub_tfvars
    assert "image_tag" not in hub_tfvars

    # 5. Cluster is tfha-agents and namespace is tfha-prod (not scion-prod / scion-hub-prod)
    assert "tfha-agents" in out
    assert "kubectl get namespace tfha-prod" in out
    assert "terraform apply" not in out


def test_setup_gke_hub_gke_mode_dry_run_supports_hub_gke_configuration(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work-gke"
    env = {
        **os.environ,
        "DRY_RUN": "1",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-central1",
        "HUB_ZONE": "us-central1-a",
        "HUB_NAME": "prod",
        "HUB_HOSTNAME": "hub.example.com",
        "HUB_IMAGE_DIGEST": "sha256:" + ("a" * 64),
        "PO_EMAIL": "admin@example.com",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "gke",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--harnesses",
            "claude,antigravity",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "configurations/shared-infra" in out
    assert "configurations/hub-gke" in out
    assert "-var-file=" in out
    assert "kubectl get namespace tfha-prod" in out
    assert "terraform apply" not in out


def test_setup_gke_hub_approve_apply_polls_async_cloud_build_and_tags_latest(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work-apply"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_log = tmp_path / "calls.log"

    # Fake build-images.sh that prints an async Cloud Build URL like upstream
    build_script = scion_repo / "image-build" / "scripts" / "build-images.sh"
    build_script.write_text(
        '#!/usr/bin/env bash\n'
        f'echo "build-images $*" >> "{calls_log}"\n'
        'echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/11111111-2222-3333-4444-555555555555]."\n'
    )
    build_script.chmod(0o755)

    flaky_marker = tmp_path / "describe_failed_once"
    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "gcloud $*" >> "{calls_log}"\n'
        'case "$*" in\n'
        '  *"builds get-default-service-account"*) echo "projects/demo/serviceAccounts/123-compute@developer.gserviceaccount.com" ;;\n'
        '  *"builds submit"*) echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]." ;;\n'
        '  *"builds describe"*)\n'
        f'    if [[ ! -f "{flaky_marker}" ]]; then touch "{flaky_marker}"; echo "ECP proxy transient error" >&2; exit 1; fi\n'
        '    echo "SUCCESS"\n'
        '    ;;\n'
        '  *"print-identity-token"*) echo "fake-oidc" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)

    (fake_bin / "terraform").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "terraform $*" >> "{calls_log}"\n'
        'if [[ "$*" == *"output -raw hub_url"* ]]; then echo ""; fi\n'
        'exit 0\n'
    )
    (fake_bin / "terraform").chmod(0o755)

    (fake_bin / "kubectl").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "kubectl $*" >> "{calls_log}"\n'
        'exit 0\n'
    )
    (fake_bin / "kubectl").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "BUILD_POLL_SEC": "0.05",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-e2e",
        "PO_EMAIL": "admin@example.com",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261027",
            "--harnesses",
            "antigravity",
            "--approve-apply",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    calls = calls_log.read_text()
    assert "projects add-iam-policy-binding demo-hub-proj --member=serviceAccount:123-compute@developer.gserviceaccount.com --role=roles/cloudbuild.builds.builder" in calls
    assert calls.index("roles/cloudbuild.builds.builder") < calls.index("builds submit")
    assert "builds describe aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in calls
    assert "builds describe 11111111-2222-3333-4444-555555555555" in calls
    assert "artifacts docker tags add us-east1-docker.pkg.dev/demo-hub-proj/tfha-scion/scion-antigravity:v20261027 us-east1-docker.pkg.dev/demo-hub-proj/tfha-scion/scion-antigravity:latest" in calls
    assert "apply -auto-approve" in calls


def test_setup_gke_hub_recovers_pass1_cloudrun_iap_audience_chicken_and_egg(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work-iap-bootstrap"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_log = tmp_path / "calls.log"
    hub_tfvars_path = work_dir / "hub.tfvars"

    build_script = scion_repo / "image-build" / "scripts" / "build-images.sh"
    build_script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/11111111-2222-3333-4444-555555555555]."\n'
    )
    build_script.chmod(0o755)

    service_created = tmp_path / "cloudrun_service_created"
    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "gcloud $*" >> "{calls_log}"\n'
        'case "$*" in\n'
        '  *"builds get-default-service-account"*) echo "projects/demo/serviceAccounts/123-compute@developer.gserviceaccount.com" ;;\n'
        '  *"builds submit"*) echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]." ;;\n'
        '  *"builds describe"*) echo "SUCCESS" ;;\n'
        '  *"artifacts docker images describe"*) echo "sha256:deadbeef" ;;\n'
        '  *"oauth-brands list"*) echo "" ;;\n'
        '  *"run services describe tfha-e2e"*)\n'
        f'    if [[ -f "{service_created}" ]]; then printf "\\t[\\"https://tfha-e2e-123456.us-east1.run.app\\"]\\n"; exit 0; fi\n'
        '    exit 1\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)

    (fake_bin / "curl").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "curl $*" >> "{calls_log}"\n'
        'printf "HTTP/2 302\\r\\nlocation: https://accounts.google.com/o/oauth2/v2/auth?client_id=123456-abcdef.apps.googleusercontent.com&redirect_uri=https%%3A%%2F%%2Ftfha-e2e-123456.us-east1.run.app\\r\\n\\r\\n"\n'
    )
    (fake_bin / "curl").chmod(0o755)

    (fake_bin / "terraform").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "terraform $*" >> "{calls_log}"\n'
        'if [[ "$*" == *"configurations/hub apply"* ]]; then\n'
        f'  touch "{service_created}"\n'
        f'  if grep -q "iap_oauth_client_id = \\"123456-abcdef.apps.googleusercontent.com\\"" "{hub_tfvars_path}"; then\n'
        '    exit 0\n'
        '  fi\n'
        '  echo "Error: hosted HA deployment requires server.auth.transport.oidc_audience" >&2\n'
        '  exit 1\n'
        'fi\n'
        'if [[ "$*" == *"output -raw hub_url"* ]]; then\n'
        f'  if grep -q "iap_oauth_client_id = \\"123456-abcdef.apps.googleusercontent.com\\"" "{hub_tfvars_path}"; then\n'
        '    echo "https://tfha-e2e-123456.us-east1.run.app"\n'
        '  else\n'
        '    echo ""\n'
        '  fi\n'
        '  exit 0\n'
        'fi\n'
        'exit 0\n'
    )
    (fake_bin / "terraform").chmod(0o755)

    (fake_bin / "kubectl").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "kubectl $*" >> "{calls_log}"\n'
        'exit 0\n'
    )
    (fake_bin / "kubectl").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "SKIP_EXISTING_IMAGES": "1",
        "BUILD_POLL_SEC": "0.05",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-e2e",
        "PO_EMAIL": "admin@example.com",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261027",
            "--harnesses",
            "antigravity",
            "--approve-apply",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert 'iap_oauth_client_id = "123456-abcdef.apps.googleusercontent.com"' in hub_tfvars_path.read_text()
    assert "HUB_URL=https://tfha-e2e-123456.us-east1.run.app" in out
    assert "HUB_IAP_AUDIENCE=123456-abcdef.apps.googleusercontent.com" in out
    assert "HUB_NAME=tfha-e2e" in out
    assert "HUB_REGION=us-east1" in out
    assert "RUNTIME_PROFILE=remote" in out


def test_setup_gke_hub_patches_cloudrun_nfs_mount_source_idempotently(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    nfs_mount_go = scion_repo / "pkg" / "runtimebroker" / "nfs_mount.go"
    nfs_mount_go.parent.mkdir(parents=True, exist_ok=True)
    original_go = (
        "package runtimebroker\n\n"
        'import "strings"\n\n'
        "func demo() {\n"
        "\tcurrentServerExport, mounted := table.Lookup(target)\n\n"
        "\tif mounted && currentServerExport == wantServerExport {\n"
        '\t\tr.setStatus(share.ID, target, true, "already mounted correctly")\n'
        "\t\treturn\n"
        "\t}\n"
        "}\n"
    )
    nfs_mount_go.write_text(original_go)

    work_dir = tmp_path / "work-nfs-patch"
    # 1. DRY_RUN=1 must never mutate SCION_REPO
    dry_env = {
        **os.environ,
        "DRY_RUN": "1",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-e2e",
        "PO_EMAIL": "admin@example.com",
    }
    dry_res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261028",
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env=dry_env,
        capture_output=True,
        text=True,
    )
    assert dry_res.returncode == 0, dry_res.stdout + dry_res.stderr
    assert nfs_mount_go.read_text() == original_go

    # 2. DRY_RUN=0 --approve-apply patches nfs_mount.go BEFORE gcloud builds submit and is idempotent
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    patched_during_submit = tmp_path / "patched_during_submit"

    build_script = scion_repo / "image-build" / "scripts" / "build-images.sh"
    build_script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/11111111-2222-3333-4444-555555555555]."\n'
    )
    build_script.chmod(0o755)

    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *"builds get-default-service-account"*) echo "projects/demo/serviceAccounts/123-compute@developer.gserviceaccount.com" ;;\n'
        '  *"builds submit"*)\n'
        f'    if grep -q "/var/lib/volumes/nfs/" "{nfs_mount_go}"; then touch "{patched_during_submit}"; fi\n'
        '    echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]."\n'
        '    ;;\n'
        '  *"builds describe"*) echo "SUCCESS" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)
    for tool in ("terraform", "kubectl"):
        (fake_bin / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / tool).chmod(0o755)

    apply_env = {
        **dry_env,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "BUILD_POLL_SEC": "0.05",
        "IAP_OAUTH_CLIENT_ID": "123456-abcdef.apps.googleusercontent.com",
    }
    for _ in range(2):
        res = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/setup-gke-hub.sh"),
                "--tier",
                "terraform-ha",
                "--hub-mode",
                "cloudrun",
                "--scion-repo",
                str(scion_repo),
                "--work-dir",
                str(work_dir),
                "--image-tag",
                "v20261028",
                "--harnesses",
                "antigravity",
                "--approve-apply",
            ],
            cwd=ROOT,
            env=apply_env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stdout + res.stderr

    assert patched_during_submit.exists(), "nfs_mount.go was not patched before gcloud builds submit"
    patched_text = nfs_mount_go.read_text()
    assert '!mounts && strings.HasPrefix(currentServerExport, "/var/lib/volumes/nfs/")' in patched_text
    assert patched_text.count("/var/lib/volumes/nfs/") == 1


def test_setup_gke_hub_patches_shared_project_clone_per_agent_idempotently(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    run_go = scion_repo / "pkg" / "agent" / "run.go"
    run_go.parent.mkdir(parents=True, exist_ok=True)
    original_run_go = (
        "package agent\n\n"
        "func demo() {\n"
        "\tif legacy := legacyAgentWorkspaceRoot(projectDir, opts.Name, agentDir, explicitWorkspace || opts.SharedWorkspace); legacy != \"\" {\n"
        "\t\troots = append(roots, legacy)\n"
        "\t}\n"
        "}\n"
    )
    run_go.write_text(original_run_go)

    projects_go = scion_repo / "pkg" / "hub" / "handlers_projects_core.go"
    projects_go.parent.mkdir(parents=True, exist_ok=True)
    original_projects_go = (
        "package hub\n\n"
        "func demo() {\n"
        "\tif project.IsSharedWorkspace() {\n"
        "\t\t_ = 1\n"
        "\t} else if project.GitRemote == \"\" {\n"
        "\t\t_ = s.initHubManagedProject(project)\n"
        "\t}\n"
        "}\n"
    )
    projects_go.write_text(original_projects_go)

    k8s_rt_go = scion_repo / "pkg" / "runtime" / "k8s_runtime.go"
    k8s_rt_go.parent.mkdir(parents=True, exist_ok=True)
    original_k8s_rt_go = (
        "package runtime\n\n"
        "func demoSharedDirs() {\n"
        "\tif config.SharedDirStorage.Shared() {\n"
        "\t\tpod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{\n"
        "\t\t\tName: volName,\n"
        "\t\t\tVolumeSource: corev1.VolumeSource{\n"
        "\t\t\t\tPersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{\n"
        "\t\t\t\t\tClaimName: claim,\n"
        "\t\t\t\t\tReadOnly:  sd.ReadOnly,\n"
        "\t\t\t\t},\n"
        "\t\t\t},\n"
        "\t\t})\n"
        "\t\tpod.Spec.Containers[0].VolumeMounts = append(pod.Spec.Containers[0].VolumeMounts, corev1.VolumeMount{\n"
        "\t\t\tName:      volName,\n"
        "\t\t\tMountPath: target,\n"
        "\t\t\tSubPath:   sdSubPath,\n"
        "\t\t\tReadOnly:  sd.ReadOnly,\n"
        "\t\t})\n"
        "\t}\n"
        "\t// Local backend per-dir PVC must keep ReadOnly: sd.ReadOnly\n"
        "\tpod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{\n"
        "\t\tName: volName,\n"
        "\t\tVolumeSource: corev1.VolumeSource{\n"
        "\t\t\tPersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{\n"
        "\t\t\t\tClaimName: pvcName,\n"
        "\t\t\t\tReadOnly:  sd.ReadOnly,\n"
        "\t\t\t},\n"
        "\t\t},\n"
        "\t})\n"
        "}\n"
    )
    k8s_rt_go.write_text(original_k8s_rt_go)

    work_dir = tmp_path / "work-run-patch"
    dry_env = {
        **os.environ,
        "DRY_RUN": "1",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-e2e",
        "PO_EMAIL": "admin@example.com",
    }
    dry_res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261028b",
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env=dry_env,
        capture_output=True,
        text=True,
    )
    assert dry_res.returncode == 0, dry_res.stdout + dry_res.stderr
    assert run_go.read_text() == original_run_go
    assert projects_go.read_text() == original_projects_go
    assert k8s_rt_go.read_text() == original_k8s_rt_go

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    patched_during_submit = tmp_path / "run_go_patched_during_submit"
    projects_patched_during_submit = tmp_path / "projects_go_patched_during_submit"
    k8s_rt_patched_during_submit = tmp_path / "k8s_rt_patched_during_submit"

    build_script = scion_repo / "image-build" / "scripts" / "build-images.sh"
    build_script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/11111111-2222-3333-4444-555555555555]."\n'
    )
    build_script.chmod(0o755)

    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *"builds get-default-service-account"*) echo "projects/demo/serviceAccounts/123-compute@developer.gserviceaccount.com" ;;\n'
        '  *"builds submit"*)\n'
        f'    if grep -q "IsSharedProjectConfigDir(projectDir)" "{run_go}"; then touch "{patched_during_submit}"; fi\n'
        f'    if grep -q "s.workspaceBackendShared()" "{projects_go}"; then touch "{projects_patched_during_submit}"; fi\n'
        f'    if ! grep -A8 "SharedDirStorage.Shared()" "{k8s_rt_go}" | grep -q "ReadOnly:  sd.ReadOnly"; then touch "{k8s_rt_patched_during_submit}"; fi\n'
        '    echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]."\n'
        '    ;;\n'
        '  *"builds describe"*) echo "SUCCESS" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)
    for tool in ("terraform", "kubectl"):
        (fake_bin / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / tool).chmod(0o755)

    apply_env = {
        **dry_env,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "BUILD_POLL_SEC": "0.05",
        "IAP_OAUTH_CLIENT_ID": "123456-abcdef.apps.googleusercontent.com",
    }
    for _ in range(2):
        res = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/setup-gke-hub.sh"),
                "--tier",
                "terraform-ha",
                "--hub-mode",
                "cloudrun",
                "--scion-repo",
                str(scion_repo),
                "--work-dir",
                str(work_dir),
                "--image-tag",
                "v20261028b",
                "--harnesses",
                "antigravity",
                "--approve-apply",
            ],
            cwd=ROOT,
            env=apply_env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stdout + res.stderr

    assert patched_during_submit.exists(), "pkg/agent/run.go was not patched before gcloud builds submit"
    assert projects_patched_during_submit.exists(), "pkg/hub/handlers_projects_core.go was not patched before gcloud builds submit"
    assert k8s_rt_patched_during_submit.exists(), "pkg/runtime/k8s_runtime.go was not patched before gcloud builds submit"
    patched_text = run_go.read_text()
    assert "config.IsSharedProjectConfigDir(projectDir)" in patched_text
    assert "config.AgentDirForProject(projectDir, opts.Name, false, hubDispatchedProjectID)" in patched_text
    assert patched_text.count("config.IsSharedProjectConfigDir(projectDir)") == 1
    patched_projects = projects_go.read_text()
    assert '} else if project.GitRemote == "" || s.workspaceBackendShared() {' in patched_projects
    assert patched_projects.count("s.workspaceBackendShared()") == 1
    patched_k8s_rt = k8s_rt_go.read_text()
    assert "ClaimName: claim,\n\t\t\t\t}" in patched_k8s_rt
    assert "SubPath:   sdSubPath,\n\t\t\tReadOnly:  sd.ReadOnly," in patched_k8s_rt
    assert "ClaimName: pvcName,\n\t\t\t\tReadOnly:  sd.ReadOnly," in patched_k8s_rt


def test_setup_gke_hub_auto_clones_scion_when_scion_repo_omitted(tmp_path):
    template_scion = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work-autoclone"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_log = tmp_path / "calls.log"

    (fake_bin / "git").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "git $*" >> "{calls_log}"\n'
        'if [[ "$1" == "clone" ]]; then\n'
        '  dest="${@: -1}"\n'
        '  mkdir -p "$dest/.git"\n'
        f'  cp -R "{template_scion}/." "$dest/"\n'
        'fi\n'
        'exit 0\n'
    )
    (fake_bin / "git").chmod(0o755)

    for tool in ("gcloud", "terraform", "kubectl"):
        (fake_bin / tool).write_text(
            '#!/usr/bin/env bash\n'
            f'echo "{tool} $*" >> "{calls_log}"\n'
            'exit 0\n'
        )
        (fake_bin / tool).chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "prod",
        "PO_EMAIL": "admin@example.com",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261110",
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    calls = calls_log.read_text()
    expected_src = str((work_dir / "scion-src").resolve())
    assert "git clone --depth 1 https://github.com/GoogleCloudPlatform/scion.git" in calls
    assert expected_src in calls
    assert (work_dir / "scion-src" / ".git").is_dir()
    assert f"-chdir={expected_src}/deploy/terraform/configurations/shared-infra" in calls

    # Second run reuses existing clone without re-cloning
    res2 = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261110",
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert calls_log.read_text().count("git clone --depth 1") == 1

    # SCION_REF passes -b <ref> on fresh work_dir
    work_dir_ref = tmp_path / "work-autoclone-ref"
    res3 = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--work-dir",
            str(work_dir_ref),
            "--image-tag",
            "v20261110",
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env={**env, "SCION_REF": "v0.9.1"},
        capture_output=True,
        text=True,
    )
    assert res3.returncode == 0, res3.stdout + res3.stderr
    assert "git clone --depth 1 -b v0.9.1 https://github.com/GoogleCloudPlatform/scion.git" in calls_log.read_text()


def test_setup_gke_hub_create_project_links_folder_billing_default_vpc_and_zonal_sql(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work-create-proj"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_log = tmp_path / "calls.log"
    proj_created = tmp_path / "proj_created"
    net_created = tmp_path / "net_created"
    sa_created = tmp_path / "sa_created"

    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "gcloud $*" >> "{calls_log}"\n'
        'case "$*" in\n'
        '  *"config get-value project"*) echo "seed-existing-proj" ;;\n'
        '  *"projects describe seed-existing-proj"*) printf "folder\\t987654\\n" ;;\n'
        '  *"projects describe demo-new-proj"*)\n'
        f'    if [[ -f "{proj_created}" ]]; then exit 0; fi\n'
        '    exit 1\n'
        '    ;;\n'
        '  *"projects create demo-new-proj"*)\n'
        f'    touch "{proj_created}"\n'
        '    exit 0\n'
        '    ;;\n'
        '  *"billing accounts list"*) echo "billingAccounts/0123-ABCD" ;;\n'
        '  *"compute networks describe default"*)\n'
        f'    if [[ -f "{net_created}" ]]; then exit 0; fi\n'
        '    exit 1\n'
        '    ;;\n'
        '  *"compute networks create default"*)\n'
        f'    touch "{net_created}"\n'
        '    exit 0\n'
        '    ;;\n'
        '  *"iam service-accounts describe factory-operator@demo-new-proj.iam.gserviceaccount.com"*)\n'
        f'    if [[ -f "{sa_created}" ]]; then exit 0; fi\n'
        '    exit 1\n'
        '    ;;\n'
        '  *"iam service-accounts create factory-operator"*)\n'
        f'    touch "{sa_created}"\n'
        '    exit 0\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)

    for tool in ("terraform", "kubectl"):
        (fake_bin / tool).write_text(
            '#!/usr/bin/env bash\n'
            f'echo "{tool} $*" >> "{calls_log}"\n'
            'exit 0\n'
        )
        (fake_bin / tool).chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "USE_OPERATOR_SA": "1",
        "HUB_PROJECT": "demo-new-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "prod",
        "PO_EMAIL": "admin@example.com",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--create-project",
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261110",
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    calls = calls_log.read_text()
    assert "projects create demo-new-proj --folder=987654" in calls
    assert "billing projects link demo-new-proj --billing-account=0123-ABCD" in calls
    assert "compute networks create default --project=demo-new-proj --subnet-mode=auto" in calls
    assert "firestore.googleapis.com" in calls
    assert "cloudtrace.googleapis.com" in calls
    assert "logging.googleapis.com" in calls
    assert "monitoring.googleapis.com" in calls
    assert "serviceusage.googleapis.com" in calls
    assert "cloudresourcemanager.googleapis.com" in calls

    shared_tfvars = (work_dir / "shared-infra.tfvars").read_text()
    assert 'sql_availability_type = "ZONAL"' in shared_tfvars

    hub_tfvars = (work_dir / "hub.tfvars").read_text()
    assert "factory-operator@demo-new-proj.iam.gserviceaccount.com" in hub_tfvars
    assert "serviceAccount:factory-operator@demo-new-proj.iam.gserviceaccount.com" in hub_tfvars
    assert "HUB_IMPERSONATE_SA=factory-operator@demo-new-proj.iam.gserviceaccount.com" in out

    # Single folder under single org fallback + explicit SQL_AVAILABILITY_TYPE override + DRY_RUN=1
    work_dir_org = tmp_path / "work-create-org-fallback"
    fake_bin_org = tmp_path / "bin-org"
    fake_bin_org.mkdir()
    (fake_bin_org / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *"config get-value project"*) echo "(unset)" ;;\n'
        '  *"projects describe demo-org-proj"*) exit 1 ;;\n'
        '  *"organizations list"*) echo "organizations/111222" ;;\n'
        '  *"resource-manager folders list --organization=111222"*) echo "folders/333444" ;;\n'
        '  *"compute networks describe default"*) exit 1 ;;\n'
        '  *"iam service-accounts describe"*) exit 1 ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin_org / "gcloud").chmod(0o755)
    res_org = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--create-project",
            "--billing-account",
            "9999-EEEE",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir_org),
            "--harnesses",
            "antigravity",
            "--dry-run",
        ],
        cwd=ROOT,
        env={
            **env,
            "PATH": f"{fake_bin_org}:{os.environ['PATH']}",
            "HUB_PROJECT": "demo-org-proj",
            "SQL_AVAILABILITY_TYPE": "REGIONAL",
        },
        capture_output=True,
        text=True,
    )
    out_org = res_org.stdout + res_org.stderr
    assert res_org.returncode == 0, out_org
    assert "+ gcloud projects create demo-org-proj --folder=333444" in out_org
    assert "+ gcloud billing projects link demo-org-proj --billing-account=9999-EEEE" in out_org
    assert 'sql_availability_type = "REGIONAL"' in (work_dir_org / "shared-infra.tfvars").read_text()


def test_setup_gke_hub_write_env_merges_existing_keys_and_sets_0600(tmp_path, fake_hub):
    scion_repo = make_fake_scion_upstream(tmp_path)
    cb_yaml = scion_repo / "image-build" / "cloudbuild-scion-base.yaml"
    cb_harness_yaml = scion_repo / "harnesses" / "antigravity" / "cloudbuild.yaml"
    cb_builder_sh = scion_repo / "image-build" / "scripts" / "builders" / "cloud-build.sh"
    cb_builder_sh.parent.mkdir(parents=True)
    for cb_file in (cb_yaml, cb_harness_yaml, cb_builder_sh):
        cb_file.write_text("steps:\n  - args:\n      - '--platform'\n      - 'linux/amd64,linux/arm64'\n")
    work_dir = tmp_path / "work-write-env"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls_log = tmp_path / "calls.log"
    env_file = tmp_path / "factory.env"
    env_file.write_text(
        "# comment\n"
        "PO_EMAIL=admin@example.com\n"
        "GITHUB_TOKEN=fake_tok_123\n"
        "HUB_URL=https://old\n"
    )
    env_file.chmod(0o644)

    build_script = scion_repo / "image-build" / "scripts" / "build-images.sh"
    build_script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/11111111-2222-3333-4444-555555555555]."\n'
    )
    build_script.chmod(0o755)

    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "gcloud $*" >> "{calls_log}"\n'
        'case "$*" in\n'
        '  *"builds get-default-service-account"*) echo "projects/demo/serviceAccounts/123-compute@developer.gserviceaccount.com" ;;\n'
        '  *"builds submit"*) echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]." ;;\n'
        '  *"builds describe"*) echo "SUCCESS" ;;\n'
        '  *"artifacts docker images describe"*) echo "sha256:deadbeef" ;;\n'
        '  *"oauth-brands list"*) echo "projects/123456/brands/123456" ;;\n'
        '  *"oauth-clients list"*) echo "projects/123456/brands/123456/identityAwareProxyClients/123456-abcdef.apps.googleusercontent.com" ;;\n'
        '  *"run services describe tfha-e2e"*) printf "\\t[\\"https://tfha-e2e-123456.us-east1.run.app\\"]\\n" ;;\n'
        '  *"print-identity-token"*) echo "fake-oidc" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)

    (fake_bin / "terraform").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "terraform $*" >> "{calls_log}"\n'
        'if [[ "$*" == *"output -raw hub_url"* ]]; then\n'
        '  echo "https://tfha-e2e-123456.us-east1.run.app"\n'
        'fi\n'
        'exit 0\n'
    )
    (fake_bin / "terraform").chmod(0o755)

    (fake_bin / "kubectl").write_text(
        '#!/usr/bin/env bash\n'
        f'echo "kubectl $*" >> "{calls_log}"\n'
        'exit 0\n'
    )
    (fake_bin / "kubectl").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DRY_RUN": "0",
        "SKIP_EXISTING_IMAGES": "1",
        "BUILD_POLL_SEC": "0.05",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-e2e",
        "HUB_API_BASE_URL": fake_hub.url,
        "HUB_TRANSPORT_TOKEN": "fake-iap-tok",
    }
    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--env",
            str(env_file),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--image-tag",
            "v20261110",
            "--harnesses",
            "antigravity",
            "--approve-apply",
            "--write-env",
            str(env_file),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert (env_file.stat().st_mode & 0o777) == 0o600

    written = env_file.read_text()
    lines = written.splitlines()
    assert lines[0] == "# comment"
    assert lines[1] == "PO_EMAIL=admin@example.com"
    assert lines[2] == "GITHUB_TOKEN=fake_tok_123"
    assert lines[3] == "HUB_URL=https://tfha-e2e-123456.us-east1.run.app"
    assert "HUB_URL=https://old" not in written
    assert "HUB_PROJECT=demo-hub-proj" in lines
    assert "SANDBOX_PROJECT=demo-hub-proj" in lines
    assert "HUB_NAME=tfha-e2e" in lines
    assert "HUB_REGION=us-east1" in lines
    assert "HUB_IAP_AUDIENCE=123456-abcdef.apps.googleusercontent.com" in lines
    assert "AGENT_RUNTIME=kubernetes" in lines
    assert "RUNTIME_PROFILE=remote" in lines
    assert "HUB_K8S_CLUSTER=tfha-agents" in lines
    assert "HUB_K8S_NAMESPACE=tfha-e2e" in lines
    assert "IMAGE_REGISTRY=us-east1-docker.pkg.dev/demo-hub-proj/tfha-scion" in lines
    assert "VERTEX_LOCATION=global" in lines

    env_puts = {
        r["path"].rsplit("/", 1)[-1]: json.loads(r["body"].decode("utf-8"))
        for r in fake_hub.requests
        if r["method"] == "PUT" and r["path"].startswith("/api/v1/env/")
    }
    assert env_puts["GOOGLE_CLOUD_PROJECT"]["value"] == "demo-hub-proj"
    assert env_puts["GOOGLE_CLOUD_REGION"]["value"] == "global"
    assert env_puts["GOOGLE_CLOUD_LOCATION"]["value"] == "global"
    # The Hub env API rejects SCION_* names as reserved (hillclimb Run 1); telemetry lives in templates.
    assert not any(k.startswith("SCION_") for k in env_puts), sorted(env_puts)

    # Upstream Cloud Build configs are narrowed to amd64 (the emulated arm64 half dominated cold start).
    # Base, per-harness and generated configs must all agree: a harness build that still asks for arm64
    # fails on the amd64-only scion-base (hillclimb Run 1 retry).
    for cb_file in (cb_yaml, cb_harness_yaml, cb_builder_sh):
        assert "linux/arm64" not in cb_file.read_text(), cb_file
        assert "'linux/amd64'" in cb_file.read_text(), cb_file

    # DRY_RUN=1 never mutates --write-env outside WORK_DIR
    untouched = tmp_path / "untouched.env"
    untouched.write_text("PO_EMAIL=admin@example.com\nHUB_URL=https://keep-me\n")
    res_dry = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(tmp_path / "work-dry-write"),
            "--harnesses",
            "antigravity",
            "--dry-run",
            "--write-env",
            str(untouched),
        ],
        cwd=ROOT,
        env={**env, "PO_EMAIL": "admin@example.com"},
        capture_output=True,
        text=True,
    )
    assert res_dry.returncode == 0, res_dry.stdout + res_dry.stderr
    assert untouched.read_text() == "PO_EMAIL=admin@example.com\nHUB_URL=https://keep-me\n"


def test_setup_gke_hub_create_project_defaults_us_east1_operator_sa_and_fails_on_iam_bind_exhaustion(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)

    # 1. Without --create-project and without HUB_REGION/SANDBOX_REGION -> keeps legacy us-central1 fallback
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HUB_REGION", "HUB_ZONE", "SANDBOX_REGION", "USE_OPERATOR_SA", "HUB_IMPERSONATE_SA")
    }
    res_legacy = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(tmp_path / "work-legacy-region"),
            "--harnesses",
            "antigravity",
            "--dry-run",
        ],
        cwd=ROOT,
        env={**clean_env, "HUB_PROJECT": "demo-legacy-proj", "PO_EMAIL": "admin@example.com"},
        capture_output=True,
        text=True,
    )
    assert res_legacy.returncode == 0, res_legacy.stdout + res_legacy.stderr
    assert "HUB_REGION=us-central1" in res_legacy.stdout
    assert "HUB_IMPERSONATE_SA=" not in res_legacy.stdout

    # 2. With --create-project (cloudrun) and no HUB_REGION/SANDBOX_REGION/USE_OPERATOR_SA -> defaults us-east1 + USE_OPERATOR_SA=1 + persists HUB_IMPERSONATE_SA
    fake_bin = tmp_path / "bin-ok"
    fake_bin.mkdir()
    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *"projects describe scion-gke-hc1-aabbcc"*) exit 0 ;;\n'
        '  *"billing accounts list"*) echo "billingAccounts/0123-ABCD" ;;\n'
        '  *"compute networks describe default"*) exit 0 ;;\n'
        '  *"iam service-accounts describe"*) exit 0 ;;\n'
        '  *"iam service-accounts add-iam-policy-binding"*) exit 0 ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)
    for tool in ("terraform", "kubectl"):
        (fake_bin / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / tool).chmod(0o755)

    write_env_path = tmp_path / "hc1.env"
    res_cp = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--create-project",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(tmp_path / "work-cp-defaults"),
            "--harnesses",
            "antigravity",
            "--write-env",
            str(write_env_path),
        ],
        cwd=ROOT,
        env={
            **clean_env,
            "PATH": f"{fake_bin}:{clean_env.get('PATH', '')}",
            "DRY_RUN": "0",
            "BUILD_POLL_SEC": "0.01",
            "HUB_PROJECT": "scion-gke-hc1-aabbcc",
            "PO_EMAIL": "admin@example.com",
        },
        capture_output=True,
        text=True,
    )
    out_cp = res_cp.stdout + res_cp.stderr
    assert res_cp.returncode == 0, out_cp
    assert "HUB_REGION=us-east1" in res_cp.stdout
    assert "HUB_IMPERSONATE_SA=factory-operator@scion-gke-hc1-aabbcc.iam.gserviceaccount.com" in res_cp.stdout
    written_lines = write_env_path.read_text().splitlines()
    assert "HUB_REGION=us-east1" in written_lines
    assert "SANDBOX_REGION=us-east1" in written_lines
    assert "HUB_IMPERSONATE_SA=factory-operator@scion-gke-hc1-aabbcc.iam.gserviceaccount.com" in written_lines

    # 3. Exhausting all 5 IAM binding attempts on OPERATOR_SA_EMAIL must exit 1 with a clear stderr message
    fake_bin_fail = tmp_path / "bin-fail"
    fake_bin_fail.mkdir()
    bind_tries = tmp_path / "bind_tries.log"
    (fake_bin_fail / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *"projects describe scion-gke-hc1-fail00"*) exit 0 ;;\n'
        '  *"billing accounts list"*) echo "billingAccounts/0123-ABCD" ;;\n'
        '  *"compute networks describe default"*) exit 0 ;;\n'
        '  *"iam service-accounts describe"*) exit 0 ;;\n'
        '  *"iam service-accounts add-iam-policy-binding"*)\n'
        f'    echo "try" >> "{bind_tries}"\n'
        '    exit 1\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin_fail / "gcloud").chmod(0o755)
    for tool in ("terraform", "kubectl"):
        (fake_bin_fail / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin_fail / tool).chmod(0o755)

    res_fail = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--create-project",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(tmp_path / "work-cp-fail"),
            "--harnesses",
            "antigravity",
        ],
        cwd=ROOT,
        env={
            **clean_env,
            "PATH": f"{fake_bin_fail}:{clean_env.get('PATH', '')}",
            "DRY_RUN": "0",
            "BUILD_POLL_SEC": "0.01",
            "HUB_PROJECT": "scion-gke-hc1-fail00",
            "PO_EMAIL": "admin@example.com",
        },
        capture_output=True,
        text=True,
    )
    assert res_fail.returncode == 1
    assert len(bind_tries.read_text().splitlines()) == 5
    assert "serviceAccountTokenCreator" in res_fail.stderr


def test_setup_gke_hub_patches_handlers_agents_core_to_skip_direct_iap_refresh_mint_idempotently(tmp_path):
    scion_repo = make_fake_scion_upstream(tmp_path)
    agents_core_go = scion_repo / "pkg" / "hub" / "handlers_agents_core.go"
    agents_core_go.parent.mkdir(parents=True, exist_ok=True)
    original_go = (
        "package hub\n\n"
        "import (\n"
        '\t"context"\n'
        '\t"time"\n'
        ")\n\n"
        "func (s *Server) handleAgentTokenRefresh() {\n"
        '\ttransportError := ""\n'
        '\tif s.transportMinter != nil && s.transportAudience != "" {\n'
        "\t\t_ = 1\n"
        "\t}\n"
        "\t_ = transportError\n"
        "}\n"
    )
    agents_core_go.write_text(original_go)

    work_dir = tmp_path / "work-agents-core"
    base_env = {
        **os.environ,
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-e2e",
        "PO_EMAIL": "admin@example.com",
    }

    # DRY_RUN=1 leaves handlers_agents_core.go untouched
    r_dry = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/setup-gke-hub.sh"),
            "--tier",
            "terraform-ha",
            "--hub-mode",
            "cloudrun",
            "--scion-repo",
            str(scion_repo),
            "--work-dir",
            str(work_dir),
            "--harnesses",
            "antigravity",
            "--dry-run",
        ],
        cwd=ROOT,
        env=base_env,
        capture_output=True,
        text=True,
    )
    assert r_dry.returncode == 0
    assert agents_core_go.read_text() == original_go

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    patched_before_build = tmp_path / "agents_core_patched_before_build"
    build_script = scion_repo / "image-build" / "scripts" / "build-images.sh"
    build_script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/11111111-2222-3333-4444-555555555555]."\n'
    )
    build_script.chmod(0o755)
    (fake_bin / "gcloud").write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *"builds get-default-service-account"*) echo "projects/demo/serviceAccounts/123-compute@developer.gserviceaccount.com" ;;\n'
        '  *"builds submit"*)\n'
        f'    if grep -q "369001918367-" "{agents_core_go}"; then touch "{patched_before_build}"; fi\n'
        '    echo "Created [https://cloudbuild.googleapis.com/v1/projects/demo/locations/global/builds/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]."\n'
        '    ;;\n'
        '  *"builds describe"*) echo "SUCCESS" ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (fake_bin / "gcloud").chmod(0o755)
    for tool in ("terraform", "kubectl"):
        (fake_bin / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / tool).chmod(0o755)

    for _ in range(2):
        r_apply = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/setup-gke-hub.sh"),
                "--tier",
                "terraform-ha",
                "--hub-mode",
                "cloudrun",
                "--scion-repo",
                str(scion_repo),
                "--work-dir",
                str(work_dir),
                "--image-tag",
                "v20261110",
                "--harnesses",
                "antigravity",
                "--approve-apply",
            ],
            cwd=ROOT,
            env={
                **base_env,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "DRY_RUN": "0",
                "BUILD_POLL_SEC": "0.01",
                "IAP_OAUTH_CLIENT_ID": "369001918367-abc.apps.googleusercontent.com",
            },
            capture_output=True,
            text=True,
        )
        assert r_apply.returncode == 0, r_apply.stdout + r_apply.stderr

    assert patched_before_build.exists()
    patched = agents_core_go.read_text()
    assert (
        'if s.transportMinter != nil && s.transportAudience != "" && !strings.HasPrefix(s.transportAudience, "369001918367-") {'
        in patched
    )
    assert patched.count('"369001918367-"') == 1
    assert patched.count('"strings"') == 1





