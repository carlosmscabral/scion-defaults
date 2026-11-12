# GKE Factory Hillclimb Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cold-start Scion GKE factory setup single-command (`gke-up` / `gke-down`), eliminate pod Direct IAP `401` and telemetry boot errors, replace `*/15` LLM cron polling with a zero-LLM Python delta controller + ephemeral single-project specialists (`$0` idle token and pod cost), and empirically hillclimb across 3 full-lifecycle E2E runs on `duboc/bananacast` using Antigravity + Gemini 4 Argon.

**Architecture:** Shell and stdlib-only Python scripts in `scripts/` and `config/harness/` deliver Direct IAP tokens to `/home/scion/.scion/transport-token` during `sciontool init` pre-start, configure GCP-native cloud telemetry (`SCION_TELEMETRY_CLOUD_PROVIDER=gcp`), automate cold-start GCP project + GKE HA creation (`setup-gke-hub.sh` + `gke_lifecycle.py`), and drive `lead` wake/sleep/rotation from a 2-minute K8s `CronJob` (`factory_delta.py`) backed by Firestore bookmarks (`factory_store.py`). Single-project kits (`eval_slug` omitted, `team.heartbeat_mode: "delta"`) compile ephemeral specialist prompts (`factory/issue-<N>`, direct `writer -> reviewer -> lead` handoff) without regressing existing two-project or scheduled kits.

**Tech Stack:** Python 3.12 stdlib (runtime), `pytest` + `pyyaml` + `jsonschema` (tests), POSIX `sh` / `bash` (`set -euo pipefail`), `gcloud`, `terraform`, `kubectl`, `gh`, Scion Hub REST API, GitHub REST API, Firestore REST API.

**Spec:** [`docs/superpowers/specs/2026-11-10-gke-factory-hillclimb-design.md`](../specs/2026-11-10-gke-factory-hillclimb-design.md)

## Global Constraints

- Work in `.worktrees/gke-factory-hillclimb` on branch `feat/gke-factory-hillclimb`. Baseline test command: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q` (`372 passed`).
- Zero secrets, PATs (`ghp_*`, `github_pat_*`, `scion_pat_*`), emails, real GCP project IDs/numbers, billing/org IDs, or VM/cluster names in tracked files (`AGENTS.md` Rule 1). Tests use `example.com` emails and fake projects (`demo-hub-proj`, `my-sandbox`, `scion-gke-hc1-a1b2c3`).
- Every new or changed script supports `DRY_RUN=1` or `--dry-run`, uses `set -euo pipefail`, and sets `CLOUDSDK_CORE_DISABLE_PROMPTS=1` (`AGENTS.md` Rule 2).
- Runtime control-plane scripts (`factory_delta.py`, `factory-watchdog.py`, `sync-factory-agents.py`, `hub_client.py`, `factory_store.py`) are Python 3.12 stdlib-only (`python:3.12-slim` container).
- Firestore holds control-plane bookmarks only (`factory_control_state/delta__<slug>`, `factory_control_state/watchdog__default`); no Spanner (`AGENTS.md` Rule 3).
- Never `docker restart` Scion agent containers (`AGENTS.md` Rule 4).
- All hillclimb roles use harness `antigravity` (`antigravity-factory` on GKE) with model `Gemini 4 Argon` and `GOOGLE_CLOUD_LOCATION=global`.
- `duboc/bananacast` `main` and `factory/main` branches must never be modified; every E2E hillclimb run uses `factory/gke-hc<k>` and deletes its ephemeral GCP project + cleans up GitHub test artifacts via `gke-down --delete-project`.
- `README.md` prose must continue passing `tests/test_docs_contract.py` (ASD-STE100: second-person `"you"`, zero contractions, `<= 25` words per sentence, all links/anchors valid).
- Conventional commits, one commit per task, zero AI trailers (`Co-Authored-By`, etc.).

## Review Focus

1. **Watchdog vs. intentional auto-sleep race:** When `factory_delta.py` stops `lead` after 10 minutes idle (`lead_sleeping: True` in `factory_control_state/delta__<slug>`), `factory-watchdog.py` (`*/5`) must NOT restart `lead` while `lead_sleeping` is `True` → Task 3, `test_watchdog_does_not_wake_lead_when_delta_marked_sleeping`.
2. **`--write-env` preserves existing secrets and comments (`0600`):** Updating `config/factory.env` after GKE bootstrap must merge/upsert discovered keys (`HUB_URL`, `HUB_IAP_AUDIENCE`, etc.) in place with mode `0600` without wiping `GITHUB_TOKEN`, `PO_EMAIL`, or comments → Task 2, `test_setup_gke_hub_write_env_merges_existing_keys_and_sets_0600`.
3. **Shared-PAT `status:po-decision` comment attribution:** Under a shared GitHub PAT (`L14`), an issue labeled `status:po-decision` with zero comments or whose latest comment contains `<!-- scion-agent:` must stay non-actionable (`po_replies == []`), whereas a newer comment without `<!-- scion-agent:` (or label `status:approved`) must flip it to actionable → Task 3, `test_classify_po_decision_distinguishes_uncommented_agent_and_human_comments`.
4. **Cold GKE metadata server during `10-direct-iap-token` pre-start:** If `gke-metadata-server` fails once transiently on pod startup it must retry and write both token files (`0600`); if metadata stays unreachable (e.g. non-GCP test container), the hook must exit `0` with a stderr warning so `sciontool init` never aborts pod startup → Task 1, `test_pre_start_direct_iap_hook_retries_metadata_and_never_bricks_pod_on_unreachable`.
5. **Safe `gke-down --delete-project` guard & unreachable-Hub resilience:** `gke-down --delete-project` must refuse deleting a GCP project whose ID does not start with `scion-gke-hc` unless `--confirm-project <id>` matches, and must still archive local state and clean up GitHub test issues/branches when the Hub URL is unreachable → Task 5, `test_gke_down_refuses_non_ephemeral_project_without_confirm_and_cleans_github_when_hub_unreachable`.

---

## File Structure

| File | Responsibility |
|---|---|
| `config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token` (new) | Pre-start hook writing Workload Identity Direct IAP OAuth2 token to `~/.scion/transport-token` and `~/.scion/iap-access-token` before `sciontool init` starts heartbeat/refresh loops. |
| `config/harness/home/.local/bin/factory-cd.sh` | Refreshes both `~/.scion/transport-token` and `~/.scion/iap-access-token` every 25m via the shared Direct IAP token logic. |
| `scripts/make-factory-harness-configs.py` | Packages `10-direct-iap-token` (`0755`) alongside `factory-cd.sh` across local, `--via-hub`, and `--via-api` modes. |
| `scripts/setup-sandbox.sh` | Grants `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/monitoring.metricWriter` to `WORKER_SA` in GKE mode. |
| `scripts/setup-gke-hub.sh` | Adds `--create-project`, `--folder-id`, `--org-id`, `--billing-account`, `--write-env`, auto-cloned `scion-src` when `--scion-repo` is omitted, and `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` + `SCION_GCP_PROJECT_ID` Hub env. |
| `scripts/factory_delta.py` (new) | Stdlib-only 2-minute delta controller: classifies GitHub issues/PO replies + Hub child pod states, wakes/rotates `lead`, and stops `lead` + idle children after grace period. |
| `scripts/factory-watchdog.py` | Recovers `phase == "halted"` (evicted) active pods in API mode while honoring `lead_sleeping` from `delta__<slug>`. |
| `scripts/install-k8s-control-plane.py` | Packages `factory_delta.py` into `ConfigMap`, adds `--enable-delta` (`scion-factory-delta` `*/2 * * * *`) and `scion-factory-github-auth` `Secret`. |
| `config/factory.schema.json` | Adds `"delta"` to `team.heartbeat_mode`, makes `eval_slug` / `eval_branch` / `eval_service` / `eval_roles` / `heartbeat_cron` optional when single-project / `delta` mode is used. |
| `scripts/role_library.py` | Adds single-project `evaluator` role (`antigravity`, `Gemini 4 Argon`) and delta-mode peer handoff rules. |
| `scripts/scion_factory.py` | `has_eval_project(spec)`, `wake_mode(spec)`, single-project + delta prompt rendering (`factory/issue-<N>`, `writer -> reviewer -> lead`, `<!-- scion-agent: ... -->`, `STATE.json`, stop idle specialists), and CLI `gke-up` / `gke-down`. |
| `scripts/provision_factory.py` | Single-project provisioning (skips `<slug>-eval` when `eval_slug` omitted), `delta` mode (skips Scion LLM schedule, enables `scion-factory-delta`), sets telemetry Hub/project env. |
| `scripts/gke_lifecycle.py` (new) | Orchestrates `gke-up` (timed `setup-gke-hub.sh` + `compile` + `provision`) and `gke-down` (pod log/transcript extraction, `scorecard.json`, GitHub test cleanup, guarded `gcloud projects delete`). |
| `presets/bananacast-gke.factory.yaml` (new) | Single-project Antigravity + `Gemini 4 Argon` delta-mode `FactorySpec` for `duboc/bananacast`. |
| `scripts/run_hillclimb_wave.py` (new) | Repeatable full-lifecycle hillclimb wave runner (`hc1`..`hc3`, idle soak verification, Run 3 eviction + PO-reply drills). |
| `docs/gke-hillclimb-report.md` (new), `docs/lessons-learned.md`, `docs/gke-setup-runbook.md`, `README.md`, `AGENTS.md` | Scorecard across Runs 1–3, `L34+` lessons learned, and updated operator docs. |

---

### Task 1: Pod Direct IAP Pre-Start Hook & Cloud Telemetry IAM

**Files:**
- Create: `config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token`
- Modify: `config/harness/home/.local/bin/factory-cd.sh:22-74`, `scripts/make-factory-harness-configs.py:31-196`, `scripts/setup-sandbox.sh:314-327`
- Test: `tests/test_direct_iap_and_telemetry.py`, `tests/test_setup_sandbox.py`

**Interfaces:**
- Produces:
  - `config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token` (POSIX `#!/bin/sh`, mode `0755`): when `SCION_TRANSPORT_MODE=iap` and `SCION_TRANSPORT_AUDIENCE` matches `369001918367-*`, queries `${SCION_METADATA_URL:-http://169.254.169.254}` then `http://metadata.google.internal` at `/computeMetadata/v1/instance/service-accounts/default/token?scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid` (up to 3 attempts per base, `SCION_IAP_RETRY_SLEEP_SEC` default `1`), atomically writes `0600` to both `${HOME}/.scion/transport-token` and `${HOME}/.scion/iap-access-token`, and always exits `0` (printing a warning to `stderr` if unreachable).
  - `factory-cd.sh`: invokes `10-direct-iap-token` (when executable, else inline fallback) before start and every `FACTORY_CD_IAP_REFRESH_SEC` (`1500`s) so both `~/.scion/transport-token` and `~/.scion/iap-access-token` stay fresh; exports `SCION_TRANSPORT_TOKEN_FILE="${HOME}/.scion/transport-token"` and unsets `SCION_TRANSPORT_TOKEN`.
  - `make-factory-harness-configs.py`: `PRE_START_IAP_SRC`, `pre_start_iap_bytes()`, copies `home/.scion/hooks/pre-start.d/10-direct-iap-token` (`0755`) in `build()`, embeds it in `via_hub()`, and uploads it with `"mode": "0755"` in `via_api()`.
  - `setup-sandbox.sh`: binds `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/monitoring.metricWriter` on `WORKER_SA` in Step 10 (`IS_GKE_MODE == 1`).

- [ ] **Step 1: Write the failing tests in `tests/test_direct_iap_and_telemetry.py` and `tests/test_setup_sandbox.py`**

```python
def test_pre_start_direct_iap_hook_writes_both_transport_and_iap_tokens_0600(fake_metadata_server, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "SCION_TRANSPORT_MODE": "iap",
        "SCION_TRANSPORT_AUDIENCE": "369001918367-abc.apps.googleusercontent.com",
        "SCION_METADATA_URL": fake_metadata_server["url"],
        "SCION_IAP_RETRY_SLEEP_SEC": "0.01",
    }
    res = subprocess.run(["sh", str(HOOK_PATH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    for name in ("transport-token", "iap-access-token"):
        p = home / ".scion" / name
        assert p.read_text() == "tok-direct-iap-ok"
        assert (p.stat().st_mode & 0o777) == 0o600
    assert "userinfo.email" in fake_metadata_server["queries"][0] and "openid" in fake_metadata_server["queries"][0]


def test_pre_start_direct_iap_hook_retries_metadata_and_never_bricks_pod_on_unreachable(fake_metadata_server, tmp_path):
    # 1) Fails once (503), succeeds on retry -> writes both files
    fake_metadata_server["fail_first"] = 1
    home1 = tmp_path / "h1"
    home1.mkdir()
    r1 = subprocess.run(
        ["sh", str(HOOK_PATH)],
        env={**os.environ, "HOME": str(home1), "SCION_TRANSPORT_MODE": "iap",
             "SCION_TRANSPORT_AUDIENCE": "369001918367-abc.apps.googleusercontent.com",
             "SCION_METADATA_URL": fake_metadata_server["url"], "SCION_IAP_RETRY_SLEEP_SEC": "0.01"},
        capture_output=True, text=True,
    )
    assert r1.returncode == 0 and (home1 / ".scion" / "transport-token").read_text() == "tok-direct-iap-ok"

    # 2) Completely unreachable metadata -> exits 0 with warning so sciontool init does not fail pod startup
    home2 = tmp_path / "h2"
    home2.mkdir()
    r2 = subprocess.run(
        ["sh", str(HOOK_PATH)],
        env={**os.environ, "HOME": str(home2), "SCION_TRANSPORT_MODE": "iap",
             "SCION_TRANSPORT_AUDIENCE": "369001918367-abc.apps.googleusercontent.com",
             "SCION_METADATA_URL": "http://127.0.0.1:1", "SCION_METADATA_FALLBACKS": "0",
             "SCION_IAP_RETRY_SLEEP_SEC": "0.01"},
        capture_output=True, text=True,
    )
    assert r2.returncode == 0
    assert "warning" in r2.stderr.lower()


def test_make_factory_harness_configs_packages_pre_start_hook_local_and_via_api(fake_hub, tmp_path):
    scion_home = tmp_path / "scion"
    hc_src = scion_home / "harness-configs" / "antigravity"
    hc_src.mkdir(parents=True)
    (hc_src / "config.yaml").write_text('harness: antigravity\ncommand:\n  base: ["agy", "chat"]\n')
    dst = Path(make_hc.build(str(scion_home), "antigravity", apply=True))
    hook_dst = dst / "home" / ".scion" / "hooks" / "pre-start.d" / "10-direct-iap-token"
    assert hook_dst.is_file() and (hook_dst.stat().st_mode & 0o777) == 0o755
    # via_api uploads home/.scion/hooks/pre-start.d/10-direct-iap-token with manifest mode "0755"
    client = HubClient(base_url=fake_hub.url, token="tok-admin")
    make_hc.via_api(client, ["antigravity"], apply=True)
    fin = next(r for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/finalize"))
    files_by_path = {f["path"]: f for f in json.loads(fin["body"].decode())["manifest"]["files"]}
    assert files_by_path["home/.scion/hooks/pre-start.d/10-direct-iap-token"]["mode"] == "0755"
```

Also add `test_setup_sandbox_gke_mode_grants_cloud_telemetry_writer_roles` in `tests/test_setup_sandbox.py` asserting `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/monitoring.metricWriter` on `agent-worker@<sandbox>.iam.gserviceaccount.com`.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_direct_iap_and_telemetry.py tests/test_setup_sandbox.py -q`
Expected: FAIL (`10-direct-iap-token` missing; telemetry roles absent).

- [ ] **Step 3: Implement `10-direct-iap-token`, `factory-cd.sh` refresh update, `make-factory-harness-configs.py` packaging, and `setup-sandbox.sh` IAM**

Honor `SCION_METADATA_FALLBACKS=0` in `10-direct-iap-token` so unit tests can disable probing `169.254.169.254` / `metadata.google.internal` when testing unreachable metadata cleanly without 10s timeouts.

- [ ] **Step 4: Run targeted and full test suites**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`
Expected: PASS (`376+ passed`).

- [ ] **Step 5: Commit**

```bash
git add config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token config/harness/home/.local/bin/factory-cd.sh scripts/make-factory-harness-configs.py scripts/setup-sandbox.sh tests/test_direct_iap_and_telemetry.py tests/test_setup_sandbox.py
git commit -m "fix(gke): refresh PID 1 Direct IAP transport-token in pre-start and grant pod telemetry roles"
```

---

### Task 2: Cold-Start GKE Hub Bootstrap (`--create-project`, Auto-Cloned Scion Source, Telemetry Env, `--write-env`)

**Files:**
- Modify: `scripts/setup-gke-hub.sh:7-65,255-285,540-596`
- Test: `tests/test_setup_gke_hub.py`

**Interfaces:**
- Produces new CLI options on `scripts/setup-gke-hub.sh`:
  - `--create-project`: when `HUB_PROJECT` does not yet exist (`gcloud projects describe "$HUB_PROJECT"` fails), resolves parent (`--folder-id <id>`, else `--org-id <id>`, else parent folder/org of active `gcloud config get-value project`, else single folder under single org), runs `gcloud projects create "$HUB_PROJECT" [--folder=|--organization=]`, links `--billing-account <id>` (or single open account from `gcloud billing accounts list --filter=open=true`), enables `cloudresourcemanager.googleapis.com` + `serviceusage.googleapis.com` + `compute.googleapis.com` + `firestore.googleapis.com` + `logging.googleapis.com` + `cloudtrace.googleapis.com` + `monitoring.googleapis.com` alongside the existing API list, creates `default` VPC network (`--subnet-mode=auto`) if `gcloud compute networks describe default` fails, creates `factory-operator@${HUB_PROJECT}.iam.gserviceaccount.com` (added to `EXTRA_ADMIN_EMAILS` + `EXTRA_IAP_MEMBERS` and written out as `HUB_IMPERSONATE_SA` when `USE_OPERATOR_SA=1`), and defaults `SQL_AVAILABILITY_TYPE=ZONAL` unless explicitly set in env.
  - Optional `--scion-repo`: when omitted, clones `SCION_GIT_URL` (default `https://github.com/GoogleCloudPlatform/scion.git`, branch/ref `SCION_REF`, `--depth 1`) into `${ABS_WORK_DIR}/scion-src` and sets `SCION_REPO="${ABS_WORK_DIR}/scion-src"`. When `--scion-repo` *is* passed, existing behavior is preserved unchanged.
  - Hub-scope telemetry env in Step 5: sets `SCION_TELEMETRY_ENABLED=true`, `SCION_TELEMETRY_CLOUD_ENABLED=true`, `SCION_TELEMETRY_CLOUD_PROVIDER=gcp`, and `SCION_GCP_PROJECT_ID=${TARGET_PROJECT}` in addition to `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION`, and `GOOGLE_CLOUD_LOCATION`.
  - `--write-env <path>`: atomically updates/creates `<path>` (`chmod 600`), preserving existing comments and unrelated keys (`GITHUB_TOKEN`, `PO_EMAIL`, etc.) while upserting `HUB_PROJECT`, `SANDBOX_PROJECT` (defaulting to `HUB_PROJECT` if unset), `HUB_URL`, `HUB_NAME`, `HUB_REGION`, `HUB_IAP_AUDIENCE`, `AGENT_RUNTIME=kubernetes`, `RUNTIME_PROFILE=remote`, `HUB_K8S_CLUSTER`, `HUB_K8S_NAMESPACE`, `IMAGE_REGISTRY`, and `VERTEX_LOCATION`.

- [ ] **Step 1: Write the failing tests in `tests/test_setup_gke_hub.py`**

Add tests:
1. `test_setup_gke_hub_auto_clones_scion_when_scion_repo_omitted(tmp_path)` — omits `--scion-repo` with a fake `git` binary creating the minimal Scion tree in `${WORK_DIR}/scion-src`; asserts `git clone --depth 1` into `scion-src` and `returncode == 0`.
2. `test_setup_gke_hub_create_project_links_folder_billing_default_vpc_and_zonal_sql(tmp_path)` — passes `--create-project` against fake `gcloud`/`terraform`/`kubectl`; asserts `projects create demo-new-proj --folder=987654`, `billing projects link demo-new-proj --billing-account=0123-ABCD`, `compute networks create default --project=demo-new-proj --subnet-mode=auto`, `sql_availability_type = "ZONAL"` in `shared-infra.tfvars`, and `firestore.googleapis.com` + `cloudtrace.googleapis.com` enabled.
3. `test_setup_gke_hub_write_env_merges_existing_keys_and_sets_0600(tmp_path)` — pre-populates `factory.env` with `# comment\nPO_EMAIL=admin@example.com\nGITHUB_TOKEN=fake_tok_123\nHUB_URL=https://old\n`; runs `--approve-apply --write-env <factory.env>`; asserts mode `0600`, comment and `GITHUB_TOKEN=fake_tok_123` preserved, `HUB_URL=https://tfha-e2e-123456.us-east1.run.app` and `HUB_IAP_AUDIENCE=123456-abcdef.apps.googleusercontent.com` updated in place, and Hub env PUTs include `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` + `SCION_GCP_PROJECT_ID`.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_setup_gke_hub.py -q`
Expected: FAIL (`Unknown option: --create-project` / `--scion-repo is required`).

- [ ] **Step 3: Implement `--create-project`, auto-clone fallback, telemetry Hub env, and `--write-env` in `scripts/setup-gke-hub.sh`**

Keep every `gcloud` call under `CLOUDSDK_CORE_DISABLE_PROMPTS=1` and `run` wrapper so `DRY_RUN=1` never mutates GCP or filesystem outside `WORK_DIR`.

- [ ] **Step 4: Run targeted and full test suites**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`
Expected: PASS (all existing + 3 new `test_setup_gke_hub.py` tests green).

- [ ] **Step 5: Commit**

```bash
git add scripts/setup-gke-hub.sh tests/test_setup_gke_hub.py
git commit -m "feat(gke): add cold-start --create-project, auto-cloned scion cache, telemetry env, and --write-env"
```

---

### Task 3: Zero-LLM Delta Controller (`factory_delta.py`), Autopilot Eviction Recovery, & K8s Control Plane Integration

**Files:**
- Create: `scripts/factory_delta.py`
- Modify: `scripts/factory-watchdog.py:920-975`, `scripts/install-k8s-control-plane.py:7-187`
- Test: `tests/test_factory_delta.py`, `tests/test_gke_watchdog_and_sync.py`

**Interfaces:**
- Produces in `scripts/factory_delta.py` (stdlib-only):
  - `AGENT_COMMENT_MARKER = "<!-- scion-agent:"`
  - `classify_github_issues(issues: list[dict], comments_by_issue: dict[int, list[dict]]) -> dict[str, list[dict]]`: returns `{"actionable": [...], "po_replies": [...], "waiting_po": [...]}`. Skips issues with labels `"digest"` or `"hillclimb-archive"`, and skips issues labeled `"status:verified"`. Issues labeled `"status:po-decision"` or `"status:blocked"` go to `"po_replies"` iff labeled `"status:approved"` OR `comments_by_issue.get(number)` is non-empty and the last comment's `body` does **not** contain `AGENT_COMMENT_MARKER`; otherwise `"waiting_po"`. All remaining open issues go to `"actionable"`.
  - `compute_delta_fingerprint(classified: dict, abnormal_children: list[dict]) -> str`: deterministic SHA-256 hex digest over sorted issue numbers/labels/comment counts + `po_replies` latest comment IDs + sorted `(slug, phase, silent_bucket)` of `abnormal_children`.
  - `run_delta_tick(client: HubClient, store, *, project_slug: str, lead_slug: str = "", github_repo: str = "", issue_label: str = "", github_token: str = "", apply: bool = False, now: float | None = None, idle_grace_sec: int = 600, max_turns_before_rotate: int = 25, child_silence_sec: int = 1800, github_fetcher=None) -> dict`:
    - State doc in Firestore: collection `"factory_control_state"`, doc_id `f"delta__{project_slug}"` (`last_fingerprint`, `idle_since`, `lead_sleeping`, `lead_turns_in_epoch`).
    - When actionable work (`actionable`, `po_replies`, or `abnormal_children`) exists and `fingerprint != last_fingerprint`:
      - If `lead_turns_in_epoch >= max_turns_before_rotate` and `running_children == []`, deletes `lead` (`DELETE /api/v1/projects/{pid}/agents/{lead_slug}`) before recreating it so `lead` starts a clean context reading `/scion-volumes/scratchpad/STATE.json`, resetting `lead_turns_in_epoch = 0`.
      - Calls `client.ensure_agent_started(pid, lead_slug, create_body=..., force_resume=True)` when `lead` is missing or `phase != "running"`, waits briefly for `tmux` (`exec_agent` `["tmux", "has-session", "-t", "scion"]`), sends one compact `DELTA:` message via `POST /api/v1/projects/{pid}/agents/{lead_slug}/message`, sets `lead_sleeping = False`, `idle_since = 0`, `last_fingerprint = fingerprint`, and increments `lead_turns_in_epoch`.
    - When `actionable == []`, `po_replies == []`, and `running_children == []`: sets `idle_since = now` if `0`; once `now - idle_since >= idle_grace_sec`, stops/deletes leftover non-lead agents and stops `lead` (`POST /api/v1/projects/{pid}/agents/{lead_slug}/stop`), setting `lead_sleeping = True` and `lead_turns_in_epoch = 0`.
- Produces in `scripts/factory-watchdog.py`:
  - `run_api_watchdog(client, state, apply=False, runtime="kubernetes", store=None)` (with `main()` passing `store=store` on API runs):
    - Treats `phase in ("error", "halted", "stopped")` as restart candidates (catching GKE Autopilot `phase == "halted"`, `exit_reason == "evicted"`), **except**:
      1. If `store` has `factory_control_state/delta__{project_slug}` with `lead_sleeping == True`, skips restarting `lead` and non-running children of that project.
      2. Ephemeral non-lead children in `phase == "stopped"` stay skipped (`not should_wake(...)`), while non-lead children in `phase in ("error", "halted")` ARE resumed (up to `MAX_RESTARTS_PER_HOUR`) so an evicted active specialist self-heals!
- Produces in `scripts/install-k8s-control-plane.py`:
  - `render_manifests(..., enable_delta: bool = False, github_repo: str = "", issue_label: str = "", lead_slug: str = "", build_branch: str = "", runtime_profile: str = "")`: includes `"factory_delta.py"` in `scion-factory-control-scripts` `ConfigMap` and, when `enable_delta=True`, renders `CronJob` `"scion-factory-delta"` (`"*/2 * * * *"`, running `["python3", "/opt/scion-factory/factory_delta.py", "--apply"]` with `GITHUB_REPO`, `ISSUE_LABEL`, `LEAD_SLUG`, `BUILD_BRANCH`, `RUNTIME_PROFILE`, and optional `GITHUB_TOKEN` from `Secret` `scion-factory-github-auth`). CLI gains `--enable-delta`, `--github-repo`, `--issue-label`, `--lead-slug`, `--build-branch`, `--runtime-profile` and writes `scion-factory-github-auth` via stdin when `GITHUB_TOKEN` is set on `--apply`.

- [ ] **Step 1: Write the failing tests in `tests/test_factory_delta.py` and `tests/test_gke_watchdog_and_sync.py`**

Pin all 6 behaviors:
1. `test_classify_po_decision_distinguishes_uncommented_agent_and_human_comments` (Review Focus #3).
2. `test_delta_tick_wakes_sleeping_lead_once_and_stays_silent_on_unchanged_fingerprint` (second tick with identical issues sends `0` messages).
3. `test_delta_tick_auto_sleeps_lead_and_stops_idle_specialists_after_grace` (`now - idle_since >= 600` calls `/stop` on `lead` and sets `lead_sleeping=True`).
4. `test_delta_tick_rotates_lead_session_after_max_turns_when_no_children_running` (`lead_turns_in_epoch == 25` deletes + recreates `lead` and resets counter).
5. `test_watchdog_recovers_halted_evicted_specialist_and_lead` (`phase="halted"` triggers `/start`).
6. `test_watchdog_does_not_wake_lead_when_delta_marked_sleeping` (Review Focus #1).
7. `test_install_k8s_control_plane_enable_delta_renders_cronjob_and_github_secret`.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_factory_delta.py tests/test_gke_watchdog_and_sync.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'factory_delta'`).

- [ ] **Step 3: Implement `scripts/factory_delta.py`, update `scripts/factory-watchdog.py`, and wire `scripts/install-k8s-control-plane.py`**

Ensure `factory_delta.py` uses only Python stdlib (`urllib.request`, `json`, `hashlib`, `time`, `argparse`, `os`, `sys`) and never logs `GITHUB_TOKEN`.

- [ ] **Step 4: Run targeted and full test suites**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/factory_delta.py scripts/factory-watchdog.py scripts/install-k8s-control-plane.py tests/test_factory_delta.py tests/test_gke_watchdog_and_sync.py
git commit -m "feat(control-plane): add zero-LLM delta controller, auto-sleep, session rotation, and eviction recovery"
```

---

### Task 4: Single-Project Specs (`eval_slug` Optional), `evaluator` Role, & Ephemeral Delta Prompts

**Files:**
- Modify: `config/factory.schema.json:17-98,266-407`, `scripts/role_library.py:47-115,311-360`, `scripts/scion_factory.py:605-770,790-905,960-1685,2074-2413`
- Test: `tests/test_single_project_delta_kit.py`, `tests/test_role_library.py`

**Interfaces:**
- Produces in `scripts/role_library.py`:
  - `ROLES["evaluator"]`: `team="build"`, `kind="catalog"`, `default_kind="evaluator"`, `harness="antigravity"`, `model="Gemini 4 Argon"`, `needs_service=False`, with both `protocol` (live + code pyramid) and `protocol_none` (L1–L5 code verification pyramid in-project on `<build_branch>`, posting structured evidence to the issue and reporting `PASS`/`FAIL` to `lead`).
- Produces in `config/factory.schema.json` & `scripts/scion_factory.py`:
  - `has_eval_project(spec: dict[str, Any]) -> bool`: `bool(str((spec.get("project") or {}).get("eval_slug") or "").strip())`.
  - `wake_mode(spec: dict[str, Any]) -> str`: `str((spec.get("team") or {}).get("heartbeat_mode") or "direct-lead")`. Valid modes: `("direct-lead", "dedicated-sensor", "delta")`.
  - When `has_eval_project(spec)` is `False`: `project.eval_slug`, `vcs.eval_branch`, `infrastructure.eval_service`, and `team.eval_roles` are optional (`normalize_team` skips `ALWAYS_EVAL` and appends `evaluator` to `build_roles` unless `evaluator` is in `dropped_defaults`; `compile_factory_boilerplate` omits `eval-workspace/` and empty `Eval Project ()` bullets).
  - When `wake_mode(spec) == "delta"`: `team.heartbeat_cron` is optional, and compiled prompts (`_render_shared_agents_md`, `_render_role_system_prompt`, `_render_chain_of_command`) emit the lean ephemeral delta protocol:
    - Writers work on `factory/issue-<N>` branched from `origin/<build_branch>`, run local tests, commit and push `origin/factory/issue-<N>` directly (no scratchpad `.patch` files, no commit-slot round-trip), and message `<reviewer>` directly.
    - Reviewer audits `origin/<build_branch>...origin/factory/issue-<N>`: on first `FAIL` messages the writer directly (`lead` not woken); on `PASS` (or second `FAIL`) writes `/scion-volumes/scratchpad/reports/<N>-review.md` and wakes `lead` once.
    - `lead` starts **only** specialists required by active issues (never pre-starting unused roles), merges verified `origin/factory/issue-<N>` onto `<build_branch>`, deletes remote `factory/issue-<N>`, persists `/scion-volumes/scratchpad/STATE.json`, stops idle specialists (`scion stop <agent>`), prepends `<!-- scion-agent: <agent-slug> -->` on GitHub comments (commenting *before* adding `status:po-decision`), and exits immediately with zero shell commands if woken by a bare `state-change` (`COMPLETED`) notification.
  - Legacy specs (`has_eval_project(spec) == True` and `wake_mode(spec) != "delta"`) compile identically to baseline so all 372 existing tests stay green.

- [ ] **Step 1: Write the failing tests in `tests/test_single_project_delta_kit.py` and `tests/test_role_library.py`**

Pin:
1. `test_evaluator_role_in_library_supports_service_and_deploy_none`.
2. `test_single_project_delta_spec_passes_schema_and_readiness_without_eval_slug_or_heartbeat_cron`.
3. `test_compiled_delta_kit_uses_issue_branches_direct_reviewer_handoff_agent_comment_marker_and_omits_eval_workspace`.
4. `test_legacy_two_project_spec_compiles_unchanged`.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_single_project_delta_kit.py tests/test_role_library.py -q`
Expected: FAIL (`unknown role 'evaluator'`, `eval_slug` required in schema).

- [ ] **Step 3: Implement schema updates, `evaluator` role, and single-project delta kit rendering**

Verify zero unrendered `${VAR}` placeholders remain in compiled templates.

- [ ] **Step 4: Run targeted and full test suites**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/factory.schema.json scripts/role_library.py scripts/scion_factory.py tests/test_single_project_delta_kit.py tests/test_role_library.py
git commit -m "feat(compiler): support single-project delta kits, evaluator role, and ephemeral specialist handoffs"
```

---

### Task 5: Single-Project Delta Provisioning & One-Command Lifecycle (`gke-up` / `gke-down`)

**Files:**
- Create: `scripts/gke_lifecycle.py`
- Modify: `scripts/provision_factory.py:100-478`, `scripts/scion_factory.py:2595-2715`
- Test: `tests/test_gke_lifecycle.py`, `tests/test_provision_factory.py`

**Interfaces:**
- Produces in `scripts/provision_factory.py`:
  - When `not scion_factory.has_eval_project(spec)`: creates/configures **only** `build_pid` across phases 4–9 (`factory_projects = [build_pid_id]`, no `eval-lead` started).
  - Sets project/Hub telemetry env `SCION_TELEMETRY_ENABLED=true`, `SCION_TELEMETRY_CLOUD_ENABLED=true`, `SCION_TELEMETRY_CLOUD_PROVIDER=gcp`, and `SCION_GCP_PROJECT_ID` (`eff_env.get("HUB_PROJECT") or sandbox_project`) on GKE apply.
  - When `scion_factory.wake_mode(spec) == "delta"`: skips creating `<slug>-lead-heartbeat` Scion schedule, passes `--enable-delta`, `--github-repo`, `--issue-label` (`scion_factory._factory_label(spec)`), `--lead-slug` (`scion_factory._agent(spec, "lead")`), `--build-branch`, and `--runtime-profile` (with `GITHUB_TOKEN` in env) to `install-k8s-control-plane.py`.
- Produces in `scripts/gke_lifecycle.py` & `scripts/scion_factory.py`:
  - `gke_up(spec_path, *, env_file, kit_dir=None, create_project=False, scion_repo=None, apply=False, send_go=False) -> dict`:
    1. Loads `spec`, derives unique harnesses from `build_roles + eval_roles` (e.g. `"antigravity"`), runs `scripts/setup-gke-hub.sh --env <env_file> --harnesses <harnesses> --write-env <env_file>` (adding `--create-project` when requested, `--approve-apply` when `apply=True`, `--dry-run` when `apply=False`), recording `setup_gke_sec`.
    2. Compiles kit into `kit_dir` (default `build/kit-<slug>`), recording `compile_sec`.
    3. Runs `provision_factory(spec, kit_dir, env_file, apply=apply, send_go=send_go)`, recording `provision_sec` and `total_sec`.
  - `extract_pod_and_transcript_metrics(backup_dir: Path) -> dict`: parses archived `agent.log` files (`iap_401_errors`, `heartbeat_errors`, `telemetry_boot_errors`) and `transcript_full.jsonl` files (`per_agent` steps, `user_inputs`, `input_tokens`, `cache_read_tokens`, `output_tokens`, plus `lead_active_vs_idle` split).
  - `gke_down(spec_path, *, env_file, backup_root=None, cleanup_issue_label="", cleanup_branch="", delete_project=False, confirm_project="", apply=False) -> dict`:
    1. Safety guard: if `delete_project=True`, requires `hub_project.startswith("scion-gke-hc")` OR `confirm_project == hub_project` (raises `ValueError` otherwise).
    2. Archives K8s pod `agent.log`s, `.gemini/.../transcript_full.jsonl`s, `CronJob` logs, and Hub agent list (tolerating unreachable Hub/cluster cleanly with recorded warnings) into `<backup_root>/<hub_project>-<ts>/` (`0700`) and writes `scorecard.json` (`0600`).
    3. When `cleanup_issue_label` / `cleanup_branch` are set and `vcs.repo` is configured: closes open test issues carrying `cleanup_issue_label` (adding label `hillclimb-archive`) and deletes remote branch `cleanup_branch` (refusing if `cleanup_branch in ("main", "master", "factory/main", "factory/eval")`).
    4. When `delete_project=True` and `apply=True`: runs `gcloud projects delete <hub_project> --quiet`.
  - CLI subcommands on `scion-factory`: `gke-up` and `gke-down` (`--json` supported).

- [ ] **Step 1: Write the failing tests in `tests/test_gke_lifecycle.py` and `tests/test_provision_factory.py`**

Pin:
1. `test_provision_single_project_delta_creates_one_project_no_schedule_and_enables_delta_cronjob`.
2. `test_gke_up_chains_setup_compile_and_provision_with_phase_timers`.
3. `test_extract_pod_and_transcript_metrics_counts_errors_and_tokens_accurately`.
4. `test_gke_down_refuses_non_ephemeral_project_without_confirm_and_cleans_github_when_hub_unreachable` (Review Focus #5, plus guard refusing deletion of `main` or `factory/main`).

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_gke_lifecycle.py tests/test_provision_factory.py -q`
Expected: FAIL (`gke_lifecycle` missing; `provision_factory` expects `eval_slug`).

- [ ] **Step 3: Implement single-project delta provisioning, `scripts/gke_lifecycle.py`, and CLI subcommands**

Ensure backup directories are created with `0o700` and zero tokens are written to `scorecard.json`.

- [ ] **Step 4: Run targeted and full test suites**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gke_lifecycle.py scripts/provision_factory.py scripts/scion_factory.py tests/test_gke_lifecycle.py tests/test_provision_factory.py
git commit -m "feat(gke): add single-project delta provisioning and scion-factory gke-up/gke-down lifecycle"
```

---

### Task 6: `bananacast` Hillclimb Wave Harness (`run_hillclimb_wave.py`) & Preset

**Files:**
- Create: `presets/bananacast-gke.factory.yaml`, `scripts/run_hillclimb_wave.py`
- Test: `tests/test_hillclimb_wave_runner.py`

**Interfaces:**
- Produces:
  - `presets/bananacast-gke.factory.yaml`: valid single-project (`eval_slug` omitted), `team.heartbeat_mode: "delta"`, `infrastructure.runtime: "kubernetes"`, `infrastructure.runtime_profile: "remote"`, `infrastructure.deploy: "none"` spec for `duboc/bananacast` with `lead`, `backend`, `frontend`, `reviewer`, `security-auditor`, and `evaluator` all pinned to `harness: "antigravity"` and `model: "Gemini 4 Argon"`.
  - `scripts/run_hillclimb_wave.py`:
    - `WAVE_CATALOG: dict[int, list[dict]]` (3 representative multi-app `bananacast` tasks per wave `1, 2, 3`, testing `src/backend` Flask + `pytest`, `src/frontend/proxy.py` / React + tests, and security/config validation against `origin/main`).
    - `run_wave(wave_num: int, *, env_file: Path, idle_soak_sec: int = 1800, drills: bool = False, apply: bool = False) -> dict`:
      1. Records `duboc/bananacast` `main` and `factory/main` head SHAs before starting.
      2. Creates remote branch `factory/gke-hc<wave_num>` from `main` SHA and seeds the 3 wave issues labeled `factory:bananacast-hc<wave_num>`.
      3. Runs `scion-factory gke-up` on a temporary wave copy of `bananacast-gke.factory.yaml` (`project.slug: bananacast-hc<wave_num>`, `vcs.build_branch: factory/gke-hc<wave_num>`) with `HUB_PROJECT=scion-gke-hc<wave_num>-<6hex>`.
      4. Polls wave issues until `status:verified` (and in Wave 3 when `drills=True`: deletes one running specialist pod via `kubectl delete pod` mid-issue to prove eviction recovery, and seeds 1 `status:po-decision` issue, verifies `lead` enters `stopped` / `0` running agent pods, posts an unmarked PO reply comment, and verifies `lead` wakes within 2 minutes and finishes).
      5. Runs the `idle_soak_sec` soak confirming `0` running agent pods and `0` additional `lead` steps/tokens after `IDLE_STOP_GRACE_SEC`.
      6. Invokes `scion-factory gke-down --delete-project --cleanup-issue-label factory:bananacast-hc<wave_num> --cleanup-branch factory/gke-hc<wave_num>` and asserts `duboc/bananacast` `main` and `factory/main` SHAs are unchanged!

- [ ] **Step 1: Write the failing tests in `tests/test_hillclimb_wave_runner.py`**

Pin:
1. `test_bananacast_gke_preset_validates_all_roles_antigravity_gemini_4_argon_and_delta_mode`.
2. `test_run_hillclimb_wave_dry_run_and_mocked_lifecycle_preserves_main_and_runs_drills`.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_hillclimb_wave_runner.py -q`
Expected: FAIL (`bananacast-gke.factory.yaml` and `run_hillclimb_wave.py` missing).

- [ ] **Step 3: Implement `presets/bananacast-gke.factory.yaml` and `scripts/run_hillclimb_wave.py`**

- [ ] **Step 4: Run targeted and full test suites**

Run: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add presets/bananacast-gke.factory.yaml scripts/run_hillclimb_wave.py tests/test_hillclimb_wave_runner.py
git commit -m "feat(hillclimb): add bananacast GKE delta preset and automated full-lifecycle wave runner"
```

---

### Task 7: Live E2E Hillclimb Runs 1, 2, & 3 (`gke-up` → Wave + Idle Soak → `gke-down --delete-project`) + Final Report & Docs

**Files:**
- Create (git-ignored): `config/hc1.env`, `config/hc2.env`, `config/hc3.env`, `~/scion-backups/scion-gke-hc*/scorecard.json`
- Create (tracked): `docs/gke-hillclimb-report.md`
- Modify (tracked): `docs/lessons-learned.md`, `docs/gke-setup-runbook.md`, `README.md`, `AGENTS.md`, `tests/test_docs_contract.py` (+ any `scripts/` refinements discovered between Runs 1→2 and Runs 2→3, each with its own unit test)

**Interfaces:**
- Consumes: `scripts/run_hillclimb_wave.py`, `scripts/gke_lifecycle.py`, `presets/bananacast-gke.factory.yaml`.
- Produces:
  - Empirical scorecards from 3 real full-lifecycle E2E runs (`scion-gke-hc1-*`, `scion-gke-hc2-*`, `scion-gke-hc3-*`), each torn down to `$0` standing cloud resources via `gke-down --delete-project`.
  - `docs/gke-hillclimb-report.md` (anonymized project IDs per `AGENTS.md` Rule 1) comparing Baseline (`bananacast` VM + `k8s-version` GKE) vs. Run 1 vs. Run 2 vs. Run 3 across cold-start time, pod error counts, `lead` active turns/tokens per issue, idle soak burn, and resilience drills.
  - `## L34.` (and any additional lessons) in `docs/lessons-learned.md`, updated `docs/gke-setup-runbook.md` (`gke-up` / `gke-down` + delta mode), and ASD-STE100-compliant updates in `README.md` + `AGENTS.md`.

- [ ] **Step 1: Execute Live Run 1 (`hc1`), archive metrics, delete `hc1` GCP project, and land Run 1 → Run 2 improvements**

Run `python3 scripts/run_hillclimb_wave.py --wave 1 --env config/hc1.env --apply`, inspect `scorecard.json` + transcripts + logs, verify `gcloud projects describe <hc1-project>` shows `DELETE_REQUESTED` and `duboc/bananacast` `main` / `factory/main` are untouched, implement + unit-test + commit the top bottlenecks surfaced in Run 1.

- [ ] **Step 2: Execute Live Run 2 (`hc2`), archive metrics, delete `hc2` GCP project, and land Run 2 → Run 3 refinements**

Run `python3 scripts/run_hillclimb_wave.py --wave 2 --env config/hc2.env --apply`, compare Run 2 vs. Run 1 scorecard (verifying `0` IAP/telemetry errors, `$0` idle soak, lower `lead` turns/tokens), verify `hc2` project deletion, and commit any Run 2 refinements with unit tests.

- [ ] **Step 3: Execute Live Run 3 (`hc3` with pod-eviction & PO-decision sleep/wake drills) and delete `hc3` GCP project**

Run `python3 scripts/run_hillclimb_wave.py --wave 3 --drills --env config/hc3.env --apply`, verify forced specialist pod deletion recovers automatically, verify `status:po-decision` puts `lead` to `0`-pod/`0`-token sleep and wakes within 2 minutes of the unmarked PO reply, and confirm `hc3` GCP project + GitHub test branches/issues are deleted.

- [ ] **Step 4: Write doc contract test & codify `docs/gke-hillclimb-report.md`, `docs/lessons-learned.md` (`L34+`), `docs/gke-setup-runbook.md`, `README.md`, and `AGENTS.md`**

Add `test_docs_cover_gke_hillclimb_delta_orchestration_and_l34` in `tests/test_docs_contract.py` (verifying `gke-up`, `gke-down`, `factory_delta.py`, `docs/gke-hillclimb-report.md`, `## L34.`, and zero leaked real GCP IDs/emails), write the docs, and run:
```bash
uv run --with pytest --with pyyaml --with jsonschema pytest tests -q
rg -i '(ghp_|github_pat_|scion_pat_|AIza|ya29\.|BEGIN PRIVATE KEY)' . --glob '!config/*.env'
```
Expected: ALL tests PASS (including `test_readme_follows_google_dev_style_and_ste100_without_content_loss`); `rg` finds zero secrets or real identifiers.

- [ ] **Step 5: Commit**

```bash
git add docs/gke-hillclimb-report.md docs/lessons-learned.md docs/gke-setup-runbook.md README.md AGENTS.md tests/test_docs_contract.py
git commit -m "docs(gke): document 3-run GKE factory hillclimb scorecard, L34 lessons, and gke-up/gke-down runbook"
```
