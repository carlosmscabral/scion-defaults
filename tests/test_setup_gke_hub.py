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


def test_setup_gke_hub_spot_workers_opt_in_configures_profiles_and_preserves_existing(tmp_path):
    import http.server
    import json
    import threading

    scion_repo = make_fake_scion_upstream(tmp_path)
    work_dir = tmp_path / "work-spot"

    # 1. Dry-run without --spot-workers keeps opt-in default (remote only)
    dry_env = {
        **os.environ,
        "DRY_RUN": "1",
        "HUB_PROJECT": "demo-hub-proj",
        "HUB_REGION": "us-east1",
        "HUB_ZONE": "us-east1-b",
        "HUB_NAME": "tfha-spot",
        "PO_EMAIL": "admin@example.com",
    }
    res_default = subprocess.run(
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
            "--dry-run",
        ],
        cwd=ROOT,
        env=dry_env,
        capture_output=True,
        text=True,
    )
    assert res_default.returncode == 0, res_default.stdout + res_default.stderr
    assert "RUNTIME_PROFILE=remote" in res_default.stdout
    assert "LEAD_PROFILE=" not in res_default.stdout
    assert "profiles 'remote' (on-demand) and 'spot'" not in res_default.stdout

    # 2. Apply with --spot-workers pushes profiles.spot + profiles.remote via Hub API while preserving existing profiles
    captured = {"env": {}, "server_config_puts": []}

    class HubStub(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path == "/api/v1/admin/server-config":
                body = json.dumps({"profiles": {"custom": {"runtime": "remote"}}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if self.path.startswith("/api/v1/env/"):
                captured["env"][self.path.rsplit("/", 1)[-1]] = payload.get("value")
            elif self.path == "/api/v1/admin/server-config":
                captured["server_config_puts"].append(payload)
            body = b'{"status":"saved"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HubStub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        hub_url = f"http://127.0.0.1:{srv.server_address[1]}"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "gcloud").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"builds get-default-service-account"* ]]; then echo ""; exit 0; fi\n'
            'if [[ "$*" == *"run services describe"* ]]; then echo "' + hub_url + '"; exit 0; fi\n'
            'if [[ "$*" == *"artifacts docker images describe"* ]]; then echo "sha256:deadbeef"; exit 0; fi\n'
            "exit 0\n"
        )
        (fake_bin / "gcloud").chmod(0o755)
        (fake_bin / "terraform").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"output -raw hub_url"* ]]; then echo "' + hub_url + '"; exit 0; fi\n'
            "exit 0\n"
        )
        (fake_bin / "terraform").chmod(0o755)
        (fake_bin / "kubectl").write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / "kubectl").chmod(0o755)

        apply_env = {
            **dry_env,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DRY_RUN": "0",
            "SKIP_EXISTING_IMAGES": "1",
            "BUILD_POLL_SEC": "0.05",
            "IAP_OAUTH_CLIENT_ID": "123456-abcdef.apps.googleusercontent.com",
            "SCION_HUB_TOKEN": "tok-admin",
        }
        res_spot = subprocess.run(
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
                "v20261111",
                "--harnesses",
                "antigravity",
                "--spot-workers",
                "--approve-apply",
            ],
            cwd=ROOT,
            env=apply_env,
            capture_output=True,
            text=True,
        )
        out = res_spot.stdout + res_spot.stderr
        assert res_spot.returncode == 0, out
        assert "RUNTIME_PROFILE=spot" in out
        assert "LEAD_PROFILE=remote" in out
        assert len(captured["server_config_puts"]) == 1
        profiles = captured["server_config_puts"][0]["profiles"]
        assert profiles["custom"] == {"runtime": "remote"}
        assert profiles["remote"] == {"runtime": "remote"}
        assert profiles["spot"]["runtime"] == "remote"
        assert profiles["spot"]["node_selector"] == {"cloud.google.com/gke-spot": "true"}
        assert profiles["spot"]["tolerations"] == [
            {
                "key": "cloud.google.com/gke-spot",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
    finally:
        srv.shutdown()
        srv.server_close()




