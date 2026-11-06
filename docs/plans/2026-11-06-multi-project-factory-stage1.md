# Multi-Project Factory — Stage 1 Implementation Plan (Shared Hub for N Factories)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run N delegated factories on the existing shared Scion HA GKE platform, each as one Scion project with its own identities, isolation enforced at pod admission, owner live edits protected and recorded as drift, one PR per issue with per-issue agents, and every factory event landing in BigQuery raw tables — proven end to end on the live platform.

**Architecture:** New stdlib-only Python modules in `scripts/` (`fleet_config`, `fleet_registry`, `fleet_policy`, `fleet_artifacts`, `fleet_events`, `drift_recorder`, `redact`, `fleet_onboard`) drive the Hub REST API, Firestore REST, `gcloud`, `kubectl` and `gh`, following the existing `hub_client`/`factory_store` patterns. Bash setup scripts keep `DRY_RUN`. Agent-side helpers (safety-gate shims and an event emitter) ship through the platform-owned `<harness>-factory` harness configs. A Kubernetes ValidatingAdmissionPolicy, parameterized from the Firestore registry, enforces tenant isolation below Scion.

**Tech Stack:** Python 3.12 stdlib (runtime), pytest + pyyaml + jsonschema (tests), bash, `gcloud`, `bq`, `kubectl` (ValidatingAdmissionPolicy v1 with CEL), Scion Hub REST API, Firestore REST, Cloud Logging, BigQuery, GitHub REST through `gh`.

**Spec:** [`docs/specs/2026-11-04-multi-project-factory-design.md`](../specs/2026-11-04-multi-project-factory-design.md) (revision 3). This plan implements Stage 1 (§6.2). Stage 2 (§6.3) and Stage 3 (§6.4) get their own plans.

**Branch:** `feat/multi-project-factory` (worktree `.worktrees/docs-multi-project-factory`). Nothing merges to `main` until Task 16 passes on the live platform.

## Global Constraints

- Every new or changed script supports `DRY_RUN=1` or `--dry-run`; `fleet-setup`, `onboard` and `retire` are dry runs unless `--apply` is given; bash uses `set -euo pipefail` and every `gcloud` call runs with `CLOUDSDK_CORE_DISABLE_PROMPTS=1`.
- No secrets, real project IDs or numbers, emails, VM or cluster names in tracked files. Tests use `example.com` emails and the fake projects `host-proj`, `sandbox-a`, `sandbox-b`.
- Runtime scripts are stdlib-only: the CronJobs run `python:3.12-slim` with scripts mounted from a ConfigMap.
- Firestore holds control-plane state only (`factory_fleet`, `factory_fleet_snapshots`, watchdog bookmarks). No Spanner, no new provisioned database.
- Never `docker restart` agent containers.
- Owner-editable artifacts (role templates, workflow hooks, non-reserved project env, schedules) are created if missing and never updated or deleted by platform tooling (§6.2.7).
- With no registry configured, the existing single-factory `provision` and env-driven control plane behave exactly as today, and the 366 existing tests stay green.
- Names: KSAs `<slug>-worker` and `<slug>-deployer`; branch `factory/issue-<N>`; agents `issue-<N>-<role>`; CronJobs `scion-fleet-sync` (`*/1 * * * *`), `scion-fleet-watchdog` (`*/5 * * * *`), `scion-fleet-drift` (`*/15 * * * *`); policy `scion-fleet-agents`; parameter ConfigMap `scion-fleet-policy-params`; Hub role `factory-owner`; dataset `factory_warehouse`; sink `scion-factory-events`; bucket `<host>-factory-transcripts`.
- Every event is one JSON object with `event_type`, `schema_version` (`1`), `event_id` (UUID4), `event_ts` (RFC 3339, UTC) and its payload fields. Agent events get their factory from trusted labels, never from the payload (§6.3.2).
- Owners are given as `--owner <email>=<github-login>`: the email is their Hub and IAP identity, the login goes into CODEOWNERS.
- One commit per task, conventional message, no AI trailers.

## Review Focus

1. Re-running `onboard` after an owner deleted a hook, env var or schedule must not recreate it: deletion is a live edit → Task 13, `test_rerun_does_not_recreate_owner_deleted_artifact`.
2. Long, uppercase or underscored slugs must be rejected or produce valid GSA and KSA names → Task 1, `test_invalid_slugs_rejected` and `test_long_slug_gets_hash_suffix`.
3. Policy parameters must be rendered from the whole registry on every onboard and retire, so a missed update can't keep a retired factory admitted → Task 4, `test_params_rendered_from_full_registry_drop_retired`; Task 13, `test_onboard_rerenders_params_from_full_registry`.
4. A registry factory whose Scion project was deleted by hand must not crash sync, watchdog or drift → Task 7, `test_fleet_jobs_skip_missing_project_with_alert`.
5. A failed event write must never block a hook or a CronJob → Task 9, `test_factory_event_never_blocks_when_receiver_down`; Task 8, `test_sync_continues_when_event_write_fails`.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/fleet_config.py` (new) | Platform and factory settings, slug validation, identity names, reserved env. |
| `config/platform.env.example` (new) | Tracked template of the git-ignored platform env file. |
| `scripts/redact.py` (new) | Secret redaction shared by drift diffs and (Stage 2) trajectories. |
| `scripts/factory_store.py` | Adds document get/upsert/list on `FirestoreStore`. |
| `scripts/fleet_registry.py` (new) | The `factory_fleet` registry and per-factory artifact snapshots. |
| `scripts/fleet_policy.py` (new) | ValidatingAdmissionPolicy, binding and parameter ConfigMap. |
| `scripts/setup-sandbox.sh` | Per-factory KSA names, host-project model and telemetry grants (fleet mode). |
| `scripts/fleet_artifacts.py` (new) | Reads, hashes and create-only writes of owner-editable artifacts. |
| `scripts/push-templates.py` | `--create-only`. |
| `scripts/fleet_events.py` (new) | Control-plane event construction and emission (stdout JSON). |
| `scripts/drift_recorder.py` (new) | Drift detection, `factory.drift` events, registry hash updates. |
| `scripts/install-k8s-control-plane.py` | `--fleet` manifests (three registry-driven CronJobs). |
| `scripts/sync-factory-agents.py` | Fleet mode: `factory.agent` events instead of Firestore status. |
| `scripts/factory-watchdog.py` | Fleet mode: registry projects, owner-pause rule, unanswered escalations. |
| `config/harness/home/.local/bin/factory-event` (new) | Agent-side event emitter to the local sciontool OTLP receiver. |
| `config/harness/home/.local/bin/factory-shims/{git,gcloud}` (new) | Platform safety gates. |
| `config/harness/home/.local/share/factory/secret-patterns.json` (new) | Secret patterns for the git shim, generated from `redact.py`. |
| `config/harness/home/.local/bin/factory-cd.sh` | Puts the shims first on `PATH`. |
| `scripts/fleet_hooks.py` (new), `config/fleet/workflow-hook.sh.tmpl` (new) | The baseline owner-editable workflow hook (pre-commit tests and lint). |
| `scripts/make-factory-harness-configs.py` | Packages shims and the event helper with exec bits. |
| `templates/lead`, `templates/_shared`, `templates/deployer`, `templates/ui-craftsman`, `templates/evaluator` (new) | One PR per issue, per-issue agents, preview revisions, escalation to all owners. |
| `scripts/role_library.py`, `scripts/scion_factory.py`, `config/factory.schema.json` | Evaluator role, kits without an Eval project, fleet kits on `<harness>-factory` configs, CLI subcommands. |
| `scripts/setup-warehouse.sh` (new) | Dataset, Log Router sink, transcript bucket, billing-export check. |
| `scripts/fleet_onboard.py` (new) | `fleet_setup`, `onboard_factory`, `retire_factory`. |
| `scripts/provision_factory.py` | Refuses on a fleet platform. |
| `AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/project-owner-guide.md` (new), `docs/fleet-runbook.md` (new), `docs/gke-setup-runbook.md` | Rule 3, fleet mode, owner guide, operator runbook. |
| `tests/conftest.py` | Shared `MemoryStore` fixture. |
| `tests/test_fleet_*.py`, `tests/test_redact.py`, `tests/test_drift_recorder.py`, `tests/test_factory_gates.py`, `tests/test_setup_warehouse.py` (new) | Tests per task. |

## Before you start

- [ ] Work in `.worktrees/docs-multi-project-factory` (under the repository root) on branch `feat/multi-project-factory`.
- [ ] Run the baseline: `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q`. Expected: `366 passed`.
- [ ] Every test command below uses that same prefix; it is written as `pytest …` for short.

---

### Task 1: Fleet configuration and identity names

**Files:**
- Create: `scripts/fleet_config.py`, `config/platform.env.example`
- Test: `tests/test_fleet_config.py`

**Interfaces:**
- Produces:
  - `PlatformConfig` (frozen dataclass): `host_project`, `region`, `hub_url`, `hub_name`, `iap_audience`, `impersonate_sa`, `namespace`, `cluster`, `image_registry`, `pvc_name`, `vertex_location`, `control_sa_name="factory-control"`, `firestore_database="(default)"`, `warehouse_dataset="factory_warehouse"`, `warehouse_location="US"`; properties `control_sa_email` and `transcript_bucket` (`f"{host_project}-factory-transcripts"`).
  - `load_platform(path: str | Path) -> PlatformConfig` reading `HUB_PROJECT`, `HUB_REGION`, `HUB_URL`, `HUB_NAME`, `HUB_IAP_AUDIENCE`, `HUB_IMPERSONATE_SA`, `HUB_K8S_NAMESPACE`, `HUB_K8S_CLUSTER`, `IMAGE_REGISTRY`, `FLEET_PVC_NAME`, `VERTEX_LOCATION` (required) and `CONTROL_SA_NAME`, `FIRESTORE_DATABASE`, `WAREHOUSE_DATASET`, `WAREHOUSE_LOCATION` (optional); raises `ValueError` naming every missing key.
  - `reserved_env(platform: PlatformConfig) -> dict[str, str]` returning exactly `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_CLOUD_REGION`, `SCION_TELEMETRY_ENABLED`, `SCION_TELEMETRY_CLOUD_ENABLED`, `SCION_GCP_PROJECT_ID`.
  - `validate_slug(slug: str) -> None` (regex `^[a-z][a-z0-9-]{1,38}[a-z0-9]$`, no `--`; raises `ValueError`).
  - `IdentityNames` (frozen dataclass): `worker_gsa_id`, `deployer_gsa_id`, `worker_ksa`, `deployer_ksa`; methods `worker_email(sandbox: str) -> str`, `deployer_email(sandbox: str) -> str`.
  - `identity_names(slug: str) -> IdentityNames`: GSA ids are `<slug>-worker` and `<slug>-deployer` when they fit in 30 characters, otherwise `<slug-prefix>-<h6>-wk` and `<slug-prefix>-<h6>-dp`, where `h6` is the first 6 hex digits of `sha256(slug)` and the prefix is cut so the id is exactly 30 characters or less; KSAs always use the full slug.
  - `Owner` (frozen dataclass): `email`, `github`; `parse_owner(text: str) -> Owner` for `email=login`.
  - `FactoryConfig` (frozen dataclass): `slug`, `sandbox_project`, `sandbox_region`, `repo`, `subdir`, `deploy`, `github_token`; `load_factory(spec: dict, env_path: str | Path) -> FactoryConfig` (`slug` from `spec["project"]["slug"]`, repo from `spec["vcs"]["repo"]`, subdir from `spec.get("monorepo", {}).get("subdir", "")`, deploy from `spec["infrastructure"].get("deploy", "service")`, sandbox and token from the env file keys `SANDBOX_PROJECT`, `SANDBOX_REGION`, `GITHUB_TOKEN`).

- [ ] **Step 1: Write the failing tests**

```python
def test_load_platform_reads_required_keys_and_defaults(tmp_path):
    p = write_env(tmp_path, FULL_PLATFORM)          # helper writes KEY=value lines, fake values only
    cfg = fleet_config.load_platform(p)
    assert cfg.host_project == "host-proj"
    assert cfg.control_sa_email == "factory-control@host-proj.iam.gserviceaccount.com"
    assert cfg.transcript_bucket == "host-proj-factory-transcripts"
    assert cfg.warehouse_dataset == "factory_warehouse" and cfg.warehouse_location == "US"

def test_load_platform_missing_keys_lists_them(tmp_path):
    with pytest.raises(ValueError, match="HUB_PROJECT.*IMAGE_REGISTRY"):
        fleet_config.load_platform(write_env(tmp_path, {"HUB_REGION": "us-east1"}))

def test_reserved_env_pins_model_and_telemetry_to_host():
    env = fleet_config.reserved_env(platform())
    assert env == {"GOOGLE_CLOUD_PROJECT": "host-proj", "GOOGLE_CLOUD_LOCATION": "global",
                   "GOOGLE_CLOUD_REGION": "global", "SCION_TELEMETRY_ENABLED": "true",
                   "SCION_TELEMETRY_CLOUD_ENABLED": "true", "SCION_GCP_PROJECT_ID": "host-proj"}

def test_identity_names_short_slug():
    n = fleet_config.identity_names("shop")
    assert (n.worker_gsa_id, n.deployer_gsa_id, n.worker_ksa, n.deployer_ksa) == \
        ("shop-worker", "shop-deployer", "shop-worker", "shop-deployer")
    assert n.worker_email("sandbox-a") == "shop-worker@sandbox-a.iam.gserviceaccount.com"

def test_long_slug_gets_hash_suffix():
    a = fleet_config.identity_names("kubernetes-apimachinery-version")
    assert len(a.worker_gsa_id) <= 30 and a.worker_gsa_id.endswith("-wk")
    assert len(a.deployer_gsa_id) <= 30 and a.deployer_gsa_id.endswith("-dp")
    assert a == fleet_config.identity_names("kubernetes-apimachinery-version")
    assert a.worker_ksa == "kubernetes-apimachinery-version-worker"

@pytest.mark.parametrize("slug", ["Shop", "a_b", "x", "ab--c", "-ab", "ab-", "a" * 41])
def test_invalid_slugs_rejected(slug):
    with pytest.raises(ValueError):
        fleet_config.validate_slug(slug)

def test_parse_owner_requires_email_and_github():
    assert fleet_config.parse_owner("ana@example.com=ana-gh") == fleet_config.Owner("ana@example.com", "ana-gh")
    with pytest.raises(ValueError):
        fleet_config.parse_owner("ana@example.com")
```

- [ ] **Step 2: Run** `pytest tests/test_fleet_config.py -q`. Expected: FAIL (`ModuleNotFoundError: fleet_config`).
- [ ] **Step 3: Implement** the interfaces above in `scripts/fleet_config.py`, and write `config/platform.env.example` with every key from `load_platform`, placeholder values in `<ANGLE_BRACKETS>`, and one comment per key.
- [ ] **Step 4: Run** `pytest tests/test_fleet_config.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git add scripts/fleet_config.py config/platform.env.example tests/test_fleet_config.py && git commit -m "feat(fleet): add platform and factory configuration with identity naming"`

---

### Task 2: Secret redaction

**Files:**
- Create: `scripts/redact.py`
- Test: `tests/test_redact.py`

**Interfaces:**
- Produces: `PLACEHOLDER = "[REDACTED]"`; `PATTERNS: list[str]` (regex sources, in this order): `ghp_[A-Za-z0-9]{20,}`, `github_pat_[A-Za-z0-9_]{20,}`, `gh[osu]_[A-Za-z0-9]{20,}`, `scion_pat_[A-Za-z0-9_-]{8,}`, `AIza[0-9A-Za-z_-]{35}`, `ya29\.[0-9A-Za-z_-]+`, `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`, `"private_key"\s*:\s*"[^"]*"`, `(?i)authorization:\s*bearer\s+\S+`, `(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*[^\s'"]+`; `Redacted` dataclass (`text: str`, `count: int`); `redact(text: str) -> Redacted`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("secret", [
    "ghp_" + "a" * 36, "github_pat_" + "B" * 30, "ghs_" + "c" * 36, "scion_pat_abcdEFGH1234",
    "AIza" + "d" * 35, "ya29." + "a0" + "x" * 12, "Authorization: Bearer abc.def", "password=hunter22"])
def test_redacts_each_token_family(secret):
    r = redact.redact(f"before {secret} after")
    assert r.count == 1 and secret not in r.text and "[REDACTED]" in r.text

def test_redacts_pem_block_and_json_private_key():
    pem = "-----BEGIN " + "PRIVATE KEY-----\\nMIIE\\n-----END " + "PRIVATE KEY-----"   # built, so scanners skip it
    assert "MIIE" not in redact.redact('{"private_key": "' + pem + '"}').text

def test_leaves_ordinary_text_alone():
    text = "git push origin factory/issue-42 && pytest -q"
    assert redact.redact(text) == redact.Redacted(text, 0)

def test_redaction_is_idempotent():
    once = redact.redact("token: abc123 ghp_" + "z" * 36)
    assert redact.redact(once.text).count == 0
```

- [ ] **Step 2: Run** `pytest tests/test_redact.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `redact()` applying `PATTERNS` in order with `re.subn`, summing counts. The placeholder must not match any pattern (idempotence).
- [ ] **Step 4: Run** `pytest tests/test_redact.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): add secret redaction for drift diffs and trajectories"` (with both files added).

---

### Task 3: Fleet registry

**Files:**
- Modify: `scripts/factory_store.py` (class `FirestoreStore`)
- Create: `scripts/fleet_registry.py`
- Modify: `config/firestore/README.md`, `config/firestore/firestore.rules`
- Modify: `tests/conftest.py` (add `MemoryStore` and fixture `memory_store`)
- Test: `tests/test_fleet_registry.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - On `FirestoreStore` (same names as the existing duck-typed fakes): `get_document(collection: str, doc_id: str) -> dict | None` (404 → `None`); `upsert_document(collection: str, doc_id: str, data: dict) -> None` (the string value `"REQUEST_TIME"` becomes a server timestamp transform, as `save_control_state` does today); `list_documents(collection: str) -> list[dict]` (follows `nextPageToken`).
  - `fleet_registry.FLEET_COLLECTION = "factory_fleet"`, `SNAPSHOT_COLLECTION = "factory_fleet_snapshots"`, `REQUIRED_FIELDS = ("slug", "project_id", "sandbox_project", "repo", "subdir", "owners", "identities", "baseline", "state")`.
  - `class FleetRegistry(store)`: `get(slug) -> dict | None`; `put(record: dict) -> None` (raises `ValueError` listing missing `REQUIRED_FIELDS`); `active() -> list[dict]` (`state == "active"`, sorted by slug); `find_by_sandbox(sandbox: str) -> dict | None` (ignores `retired`); `record_hashes(slug: str, hashes: dict[str, str], checked_at: str) -> None`; `mark_retired(slug: str) -> None`; `platform_paused(slug: str) -> set[str]`; `set_platform_paused(slug: str, names: set[str]) -> None`; `get_snapshot(slug: str) -> dict[str, str]`; `put_snapshot(slug: str, contents: dict[str, str]) -> None` (a value longer than 200 000 characters is stored as `"sha256:<hex>"` instead).
  - `tests/conftest.py`: `class MemoryStore` with `get_document`, `upsert_document`, `list_documents`; fixture `memory_store`.

- [ ] **Step 1: Write the failing tests**

```python
def record(**over):
    base = {"slug": "shop", "project_id": "p-shop", "sandbox_project": "sandbox-a", "repo": "acme/shop",
            "subdir": "", "owners": [{"email": "ana@example.com", "github": "ana-gh"}],
            "identities": {"worker_ksa": "shop-worker"}, "baseline": {"commit": "abc", "hashes": {}},
            "state": "active"}
    return {**base, **over}

def test_put_requires_core_fields(memory_store):
    with pytest.raises(ValueError, match="sandbox_project"):
        FleetRegistry(memory_store).put({"slug": "shop"})

def test_active_excludes_retired_and_paused(memory_store):
    reg = FleetRegistry(memory_store)
    for slug, state in (("a1", "active"), ("b1", "retired"), ("c1", "paused")):
        reg.put(record(slug=slug, state=state, sandbox_project=f"sb-{slug}"))
    assert [r["slug"] for r in reg.active()] == ["a1"]

def test_find_by_sandbox_ignores_retired(memory_store):
    reg = FleetRegistry(memory_store)
    reg.put(record(slug="old", state="retired"))
    assert reg.find_by_sandbox("sandbox-a") is None

def test_snapshot_caps_large_values(memory_store):
    reg = FleetRegistry(memory_store)
    reg.put_snapshot("shop", {"template:lead/system-prompt.md": "x" * 200_001, "hook:h": "echo hi"})
    snap = reg.get_snapshot("shop")
    assert snap["template:lead/system-prompt.md"].startswith("sha256:") and snap["hook:h"] == "echo hi"

def test_firestore_store_document_methods_over_http(monkeypatch):
    # monkeypatch FirestoreStore._request with a recorder: GET 404 -> None; upsert sends a commit with
    # an update for factory_fleet/shop; list_documents follows one nextPageToken and returns both pages.
    ...

def test_firestore_rules_deny_client_access_to_fleet_collections():
    rules = (ROOT / "config/firestore/firestore.rules").read_text()
    for coll in ("factory_fleet", "factory_fleet_snapshots"):
        assert f"match /{coll}/{{doc}}" in rules
    assert rules.count("allow read, write: if false") >= 2
```

The `...` test body is a recorder with three canned responses; assert the HTTP methods and paths it saw.

- [ ] **Step 2: Run** `pytest tests/test_fleet_registry.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the three `FirestoreStore` methods, `FleetRegistry`, `MemoryStore`; add both collections to `firestore.rules` (no client access) and to the collection table in `config/firestore/README.md`.
- [ ] **Step 4: Run** `pytest tests/test_fleet_registry.py tests/test_firestore_control_plane.py tests/test_gke_watchdog_and_sync.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): add the Firestore fleet registry and artifact snapshots"`

---

### Task 4: Admission policy

**Files:**
- Create: `scripts/fleet_policy.py`
- Test: `tests/test_fleet_policy.py`

**Interfaces:**
- Consumes: `PlatformConfig`, `reserved_env`, `identity_names` (Task 1); registry records (Task 3).
- Produces:
  - `POLICY_NAME = "scion-fleet-agents"`, `BINDING_NAME = "scion-fleet-agents-binding"`, `PARAMS_NAME = "scion-fleet-policy-params"`.
  - `render_policy(platform: PlatformConfig) -> list[dict]`: a `ValidatingAdmissionPolicy` (`admissionregistration.k8s.io/v1`) matching `pods` on `CREATE`, `objectSelector` `scion.agent: "true"`, `paramKind` ConfigMap, `failurePolicy: Fail`, `variables` `pid` (the pod's `scion.project_id` label, or `""`); and a binding with `paramRef` (`name` `PARAMS_NAME`, `namespace` the runtime namespace, `parameterNotFoundAction: Deny`), `matchResources.namespaceSelector` on `kubernetes.io/metadata.name`, `validationActions: [Deny]`.
  - `render_params(platform: PlatformConfig, records: list[dict]) -> dict`: a ConfigMap in the runtime namespace with `data` keys `image_prefix` (`f"{image_registry}/"`), `pvc`, `env.<NAME>` for each reserved env var, and for each **active** record with project id `P` (from `record["project_id"]`, KSA names from `identity_names(record["slug"])`): `ksa.<P>` = `"<worker_ksa>,<deployer_ksa>"` and `prefix.<P>` = `"projects/<P>/"`.
  - Five validations, in this order, each `message` starting with its name: `registered` (the `ksa.<pid>` key exists), `service account` (`spec.serviceAccountName` is in the split `ksa.<pid>` list), `image` (every container and init container image starts with `image_prefix`), `reserved env` (a container env var whose `env.<NAME>` key exists has exactly that value), `volume` (every volume is `projected`, `emptyDir`, or a PVC whose `claimName` equals `pvc`; every mount of a PVC volume has a `subPath` starting with `prefix.<pid>`).

- [ ] **Step 1: Write the failing tests**

```python
def test_policy_targets_agent_pods_in_runtime_namespace_and_denies_on_failure():
    vap, binding = fleet_policy.render_policy(platform())
    rule = vap["spec"]["matchConstraints"]["resourceRules"][0]
    assert rule["resources"] == ["pods"] and rule["operations"] == ["CREATE"]
    assert vap["spec"]["matchConstraints"]["objectSelector"]["matchLabels"] == {"scion.agent": "true"}
    assert vap["spec"]["failurePolicy"] == "Fail"
    assert binding["spec"]["paramRef"] == {"name": "scion-fleet-policy-params", "namespace": "tfha-e2e",
                                           "parameterNotFoundAction": "Deny"}
    assert binding["spec"]["validationActions"] == ["Deny"]

def test_policy_has_five_named_rules():
    vap, _ = fleet_policy.render_policy(platform())
    names = [v["message"].split(":")[0] for v in vap["spec"]["validations"]]
    assert names == ["registered", "service account", "image", "reserved env", "volume"]

def test_params_for_two_factories():
    cm = fleet_policy.render_params(platform(), [rec("shop", "p1"), rec("blog", "p2")])
    d = cm["data"]
    assert d["ksa.p1"] == "shop-worker,shop-deployer" and d["prefix.p2"] == "projects/p2/"
    assert d["image_prefix"] == "us-east1-docker.pkg.dev/host-proj/scion/" and d["pvc"] == "fleet-nfs"
    assert d["env.GOOGLE_CLOUD_PROJECT"] == "host-proj"

def test_params_rendered_from_full_registry_drop_retired():
    d = fleet_policy.render_params(platform(), [rec("shop", "p1"), rec("old", "p9", state="retired")])["data"]
    assert "ksa.p1" in d and "ksa.p9" not in d and "prefix.p9" not in d

def test_render_is_deterministic():
    a = json.dumps(fleet_policy.render_params(platform(), [rec("shop", "p1")]), sort_keys=True)
    assert a == json.dumps(fleet_policy.render_params(platform(), [rec("shop", "p1")]), sort_keys=True)
```

- [ ] **Step 2: Run** `pytest tests/test_fleet_policy.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** both renderers. Write the CEL with the Kubernetes strings extension (`split`, `startsWith`); for example the `service account` rule is `params.data['ksa.' + variables.pid].split(',').exists(k, k == object.spec.serviceAccountName)`. Guard every rule so it evaluates to `false` (deny) rather than erroring when `pid` is `""`.
- [ ] **Step 4: Run** `pytest tests/test_fleet_policy.py -q`. Expected: PASS. (The CEL itself is proven against the live API server in Task 16, step 5.)
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): render the admission policy that pins agent pods to their factory"`

---

### Task 5: Per-factory identities in `setup-sandbox.sh`

**Files:**
- Modify: `scripts/setup-sandbox.sh` (Workload Identity block, worker and control SA block around lines 300–400)
- Test: `tests/test_setup_sandbox.py`

**Interfaces:**
- Produces new inputs, each defaulting to today's behavior: `WORKER_KSA_NAME` (default `scion-worker`), `DEPLOYER_KSA_NAME` (default `scion-deployer`), `MODEL_PROJECT` (default the sandbox), `TELEMETRY_PROJECT` (default empty: no grants), `FLEET_MODE` (default `0`). With `FLEET_MODE=1`: no control SA is created or granted; the worker gets `roles/aiplatform.user` on `MODEL_PROJECT` and not on the sandbox; the worker gets `roles/logging.logWriter`, `roles/monitoring.metricWriter` and `roles/cloudtrace.agent` on `TELEMETRY_PROJECT`.

- [ ] **Step 1: Write the failing tests** (reuse the file's fake `gcloud`/`kubectl` harness and its `calls` log)

```python
def test_fleet_mode_uses_per_factory_ksas_and_host_model_access(tmp_path):
    calls = run_sandbox(tmp_path, FLEET_MODE="1", AGENT_RUNTIME="kubernetes", SANDBOX_PROJECT="sandbox-a",
                        HUB_PROJECT="host-proj", HUB_K8S_NAMESPACE="tfha-e2e",
                        WORKER_SA_NAME="shop-worker", WORKER_KSA_NAME="shop-worker",
                        MODEL_PROJECT="host-proj", TELEMETRY_PROJECT="host-proj")
    assert "serviceAccount:host-proj.svc.id.goog[tfha-e2e/shop-worker]" in calls
    assert re.search(r"projects add-iam-policy-binding host-proj .*shop-worker@sandbox-a.*roles/aiplatform.user", calls)
    for role in ("roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/cloudtrace.agent"):
        assert re.search(rf"projects add-iam-policy-binding host-proj .*shop-worker@sandbox-a.*{role}", calls)
    assert not re.search(r"projects add-iam-policy-binding sandbox-a .*shop-worker@sandbox-a.*roles/aiplatform.user", calls)
    assert "factory-control" not in calls

def test_kubernetes_mode_defaults_keep_scion_worker_ksa(tmp_path):
    calls = run_sandbox(tmp_path, AGENT_RUNTIME="kubernetes", SANDBOX_PROJECT="sandbox-a",
                        HUB_PROJECT="host-proj", HUB_K8S_NAMESPACE="tfha-e2e")
    assert "[tfha-e2e/scion-worker]" in calls
```

`run_sandbox` is a small helper in the test file that runs the script with `DRY_RUN=0`, the fake binaries on `PATH`, and returns the `calls` log text; extract it from the existing tests if they inline the same code.

- [ ] **Step 2: Run** `pytest tests/test_setup_sandbox.py -q`. Expected: the two new tests FAIL, the existing ones PASS.
- [ ] **Step 3: Implement** the inputs in `setup-sandbox.sh`, replacing the hard-coded `scion-worker`/`scion-deployer` with the variables and branching on `FLEET_MODE` for the control SA and grants.
- [ ] **Step 4: Run** `pytest tests/test_setup_sandbox.py -q && DRY_RUN=1 FACTORY_ENV=/dev/null bash -n scripts/setup-sandbox.sh`. Expected: PASS, no syntax error.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): per-factory KSAs and host-project model access in setup-sandbox"`

---

### Task 6: Owner-editable artifacts and create-only writes

**Files:**
- Create: `scripts/fleet_artifacts.py`
- Modify: `scripts/push-templates.py` (add `--create-only`)
- Modify: `tests/test_hub_client.py` (fake hub: `GET`/`POST /api/v1/projects/{id}/env`, `GET`/`POST /api/v1/projects/{id}/pre-start-hooks`, `GET /api/v1/templates?projectId={id}` with file download, plus `seed_env`, `seed_hook`, `seed_template` helpers)
- Test: `tests/test_fleet_artifacts.py`

**Interfaces:**
- Consumes: `HubClient.request` (existing).
- Produces:
  - Artifact keys: `template:<template-name>/<relative-path>`, `hook:<hook-slug>`, `schedule:<schedule-name>`, `env:<KEY>`.
  - `read_artifacts(client, project_id: str) -> dict[str, str]`: key → content; a schedule's content is canonical JSON of `cronExpr`, `message`, `status`; env vars the API marks secret are left out.
  - `hash_artifacts(contents: dict[str, str]) -> dict[str, str]`: key → `sha256` hex of the UTF-8 content.
  - `CreateReport` dataclass: `created: list[str]`, `left_alone: list[str]`, `skipped_deleted: list[str]`.
  - `create_missing(client, project_id: str, desired: dict[str, dict], baseline_keys: set[str]) -> CreateReport`: `desired` maps `hook:`/`env:`/`schedule:` keys to API bodies; a key is created only when it is absent from the Hub **and** absent from `baseline_keys`; a key in `baseline_keys` but absent from the Hub is reported in `skipped_deleted`; the function never sends `PUT`, `PATCH` or `DELETE`.
  - `push-templates.py --create-only`: a template that already exists for the project is reported as `left alone: <role>` and gets no upload or finalize request.

- [ ] **Step 1: Write the failing tests**

```python
def test_read_artifacts_covers_templates_hooks_schedules_env(fake_hub):
    seed_factory(fake_hub, "p1")   # one template lead with 2 files, hook guard, schedule shop-lead-heartbeat, env FOO
    keys = set(fleet_artifacts.read_artifacts(client(fake_hub), "p1"))
    assert keys == {"template:lead/system-prompt.md", "template:lead/scion-agent.yaml", "hook:guard",
                    "schedule:shop-lead-heartbeat", "env:FOO"}

def test_hash_artifacts_is_stable():
    h = fleet_artifacts.hash_artifacts({"hook:a": "echo hi"})
    assert h == {"hook:a": hashlib.sha256(b"echo hi").hexdigest()}

def test_create_missing_creates_only_absent(fake_hub):
    fake_hub.seed_env("p1", "FOO", "1")
    rep = fleet_artifacts.create_missing(client(fake_hub), "p1",
        {"env:FOO": {"key": "FOO", "value": "2"}, "env:BAR": {"key": "BAR", "value": "3"}}, set())
    assert rep.created == ["env:BAR"] and rep.left_alone == ["env:FOO"]
    assert not [r for r in fake_hub.requests if r["method"] in ("PUT", "PATCH", "DELETE")]

def test_create_missing_respects_owner_deletion(fake_hub):
    rep = fleet_artifacts.create_missing(client(fake_hub), "p1", {"hook:guard": {"slug": "guard"}}, {"hook:guard"})
    assert rep.skipped_deleted == ["hook:guard"] and rep.created == []

def test_push_templates_create_only_leaves_existing_template_untouched(fake_hub, tmp_path):
    # existing template "lead" for p1; run push-templates.py --direct --apply --create-only --roles lead
    # assert stdout contains "left alone: lead" and no POST .../upload or .../finalize request was made
    ...
```

- [ ] **Step 2: Run** `pytest tests/test_fleet_artifacts.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the fake-hub routes, `fleet_artifacts.py`, and `--create-only` in `push-templates.py` (both the direct API path and the remote path take the flag).
- [ ] **Step 4: Run** `pytest tests/test_fleet_artifacts.py tests/test_hub_client.py tests/test_remote_hub_scripts.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): read, hash and create-only write owner-editable artifacts"`

---

### Task 7: Control-plane events and the drift recorder

**Files:**
- Create: `scripts/fleet_events.py`, `scripts/drift_recorder.py`
- Test: `tests/test_drift_recorder.py`

**Interfaces:**
- Consumes: `redact` (Task 2), `FleetRegistry` (Task 3), `read_artifacts`, `hash_artifacts` (Task 6), `reserved_env` keys (Task 1).
- Produces:
  - `fleet_events.make_event(event_type: str, factory: str, **fields) -> dict` (adds `schema_version`, `event_id`, `event_ts`, `factory`); `fleet_events.emit(event: dict, stream=sys.stdout) -> bool` (one JSON line; `False` on any write error, never raises).
  - `DriftChange` dataclass: `key: str`, `kind: str` (`added` | `changed` | `deleted`), `old_hash: str | None`, `new_hash: str | None`.
  - `detect_drift(previous: dict[str, str], current: dict[str, str]) -> list[DriftChange]` (sorted by key).
  - `drift_event(factory: str, change: DriftChange, old_text: str, new_text: str, reserved_env_keys: set[str]) -> dict`: a `factory.drift` event with `artifact`, `kind`, `old_hash`, `new_hash`, `reserved_field` (`True` when a template change touches a line containing `kubernetes:`, `image:`, `volumes:`, `serviceAccountName` or `service_account`, or an `env:` key is in `reserved_env_keys`), `diff` (unified diff, redacted, capped at 65 536 characters with the suffix `\n...[truncated]`) and `redactions` (count).
  - `run(registry, client, apply: bool, stream=sys.stdout) -> int`: for each active factory, read and hash its artifacts, emit one event per change (using the stored snapshot as the old text); with `apply`, write the new hashes and snapshot. A factory whose Scion project returns 404 gets one `factory.fleet_error` event and is skipped. Returns 0.
  - CLI: `python3 scripts/drift_recorder.py [--apply]` using `HubClient.from_env()` and `factory_store.store_from_env()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_detect_drift_added_changed_deleted():
    out = drift_recorder.detect_drift({"a": "1", "b": "2"}, {"b": "3", "c": "4"})
    assert [(c.key, c.kind) for c in out] == [("a", "deleted"), ("b", "changed"), ("c", "added")]

def test_drift_event_redacts_and_caps_diff():
    ch = drift_recorder.DriftChange("hook:guard", "changed", "h1", "h2")
    ev = drift_recorder.drift_event("shop", ch, "echo old\n", "echo token=abc123\n" + "y" * 70_000, set())
    assert ev["event_type"] == "factory.drift" and "abc123" not in ev["diff"] and ev["redactions"] == 1
    assert len(ev["diff"]) <= 65_536 + len("\n...[truncated]") and ev["diff"].endswith("...[truncated]")

def test_reserved_field_flag_for_template_kubernetes_block_and_reserved_env():
    t = drift_recorder.DriftChange("template:lead/scion-agent.yaml", "changed", "a", "b")
    assert drift_recorder.drift_event("shop", t, "x: 1\n", "kubernetes:\n  serviceAccountName: blog-worker\n",
                                      set())["reserved_field"] is True
    e = drift_recorder.DriftChange("env:GOOGLE_CLOUD_PROJECT", "changed", "a", "b")
    assert drift_recorder.drift_event("shop", e, "a", "b", {"GOOGLE_CLOUD_PROJECT"})["reserved_field"] is True

def test_run_updates_hashes_only_with_apply(fake_hub, memory_store):
    reg = registry_with_factory(memory_store, "shop", "p1", hashes={})
    seed_factory(fake_hub, "p1")
    drift_recorder.run(reg, client(fake_hub), apply=False, stream=io.StringIO())
    assert reg.get("shop").get("current_hashes") in (None, {})
    drift_recorder.run(reg, client(fake_hub), apply=True, stream=io.StringIO())
    assert reg.get("shop")["current_hashes"]["hook:guard"]

def test_fleet_jobs_skip_missing_project_with_alert(fake_hub, memory_store):
    reg = registry_with_factory(memory_store, "gone", "p-missing", hashes={})
    out = io.StringIO()
    assert drift_recorder.run(reg, client(fake_hub), apply=True, stream=out) == 0
    assert json.loads(out.getvalue().splitlines()[0])["event_type"] == "factory.fleet_error"
```

- [ ] **Step 2: Run** `pytest tests/test_drift_recorder.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `fleet_events.py` and `drift_recorder.py`. `record_hashes` stores the current hashes under `current_hashes` and the time under `checked_at`; the baseline in `baseline.hashes` never changes after onboarding.
- [ ] **Step 4: Run** `pytest tests/test_drift_recorder.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): record owner live edits as factory.drift events"`

---

### Task 8: Registry-driven control plane

**Files:**
- Modify: `scripts/install-k8s-control-plane.py`, `scripts/sync-factory-agents.py`, `scripts/factory-watchdog.py`
- Test: `tests/test_fleet_control_plane.py` (new); `tests/test_gke_watchdog_and_sync.py` stays unchanged and green

**Interfaces:**
- Consumes: `FleetRegistry` (Task 3), `fleet_events` (Task 7).
- Produces:
  - `install-k8s-control-plane.py`: `render_fleet_manifests(namespace: str, hub_url: str, firestore_project: str, firestore_database: str, gsa_email: str, iap_audience: str, image: str = "python:3.12-slim") -> list[dict]` returning the KSA `scion-factory-control`, the ConfigMap `scion-fleet-control-scripts` (`hub_client.py`, `factory_store.py`, `fleet_config.py`, `fleet_registry.py`, `fleet_events.py`, `fleet_artifacts.py`, `redact.py`, `drift_recorder.py`, `sync-factory-agents.py`, `factory-watchdog.py`) and the three CronJobs from the Global Constraints, each with env `FLEET_REGISTRY=firestore`, `FIRESTORE_PROJECT`, `FIRESTORE_DATABASE`, `HUB_URL`, `HUB_IAP_AUDIENCE`, `AGENT_RUNTIME=kubernetes` and the existing optional token secret. New CLI flag `--fleet`.
  - `sync-factory-agents.py`: `issue_number(agent_name: str) -> int | None` (`issue-42-backend` → `42`); with `FLEET_REGISTRY=firestore` it reads `FleetRegistry.active()` and emits one `factory.agent` event per agent (`factory`, `agent_id`, `agent`, `template`, `harness`, `model`, `phase`, `activity`, `issue`, `branch`, `created_at`, `updated_at`) instead of writing Firestore status; a failed emit is reported on stderr and the run still exits 0.
  - `factory-watchdog.py`: with `FLEET_REGISTRY=firestore`, projects come from the registry; the schedule guard resumes a paused schedule only when its `statusReason` is non-empty or its name is in `registry.platform_paused(slug)`; the inbox check is skipped; new `check_unanswered_escalations(entries: list[dict], now: datetime, max_age_hours: int = 24) -> list[dict]` (an `factory.escalation` entry with no later `factory.escalation_resolved` entry for the same `escalation_id`, older than the limit) and `fetch_escalation_entries(project: str, since: datetime) -> list[dict]` (Cloud Logging `entries:list` over REST with the metadata-server token); each result raises `log_alert("ESCALATION_UNANSWERED", ...)` once (bookmarked in the watchdog state).

- [ ] **Step 1: Write the failing tests**

```python
def test_fleet_manifests_have_no_factory_values():
    ms = install_k8s.render_fleet_manifests("tfha-e2e", "https://hub.example.com", "host-proj", "(default)",
                                            "factory-control@host-proj.iam.gserviceaccount.com", "aud")
    cjs = {m["metadata"]["name"]: m for m in ms if m["kind"] == "CronJob"}
    assert {n: c["spec"]["schedule"] for n, c in cjs.items()} == {
        "scion-fleet-sync": "*/1 * * * *", "scion-fleet-watchdog": "*/5 * * * *", "scion-fleet-drift": "*/15 * * * *"}
    for c in cjs.values():
        env = {e["name"] for e in c["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert "FLEET_REGISTRY" in env and not env & {"FACTORY_PROJECTS", "SANDBOX_PROJECT"}

def test_sync_fleet_mode_emits_one_agent_event_per_agent_with_issue_number(fake_hub, memory_store, capsys): ...
def test_sync_continues_when_event_write_fails(fake_hub, memory_store, monkeypatch): ...
def test_watchdog_fleet_mode_keeps_owner_paused_schedule(fake_hub, memory_store): ...
def test_watchdog_fleet_mode_resumes_platform_or_scion_paused(fake_hub, memory_store): ...

def test_check_unanswered_escalations_flags_only_old_unresolved():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    entries = [esc("e1", now - timedelta(hours=30)), esc("e2", now - timedelta(hours=30)),
               resolved("e2", now - timedelta(hours=2)), esc("e3", now - timedelta(hours=3))]
    assert [e["escalation_id"] for e in watchdog_mod.check_unanswered_escalations(entries, now)] == ["e1"]
```

Write the four `...` tests with the fake hub and `memory_store`: two agents `issue-7-backend` and `shop-lead` give two `factory.agent` lines with `issue` 7 and `None`; a patched `fleet_events.emit` returning `False` still yields exit code 0; a schedule with `status: paused` and empty `statusReason` gets no resume request, while one with `statusReason: creator_deleted`, or listed in `platform_paused`, gets `POST .../resume`.

- [ ] **Step 2: Run** `pytest tests/test_fleet_control_plane.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** fleet mode in the three scripts, keeping every legacy code path unchanged when `FLEET_REGISTRY` is unset.
- [ ] **Step 4: Run** `pytest tests/test_fleet_control_plane.py tests/test_gke_watchdog_and_sync.py tests/test_watchdog_wake.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): drive sync, watchdog and drift CronJobs from the registry"`

---

### Task 9: Agent-side hooks: safety gates, workflow hook and event helper

**Files:**
- Create: `config/harness/home/.local/bin/factory-event`, `config/harness/home/.local/bin/factory-shims/git`, `config/harness/home/.local/bin/factory-shims/gcloud`, `config/harness/home/.local/share/factory/secret-patterns.json`, `scripts/fleet_hooks.py`, `config/fleet/workflow-hook.sh.tmpl`
- Modify: `config/harness/home/.local/bin/factory-cd.sh`, `scripts/make-factory-harness-configs.py`
- Test: `tests/test_factory_gates.py`

**Interfaces:**
- Consumes: `redact.PATTERNS` (Task 2) as the source of `secret-patterns.json`.
- Produces:
  - `factory-event <event_type> [json-object]` (python3, stdlib): posts one OTLP/HTTP JSON log record whose body is the JSON event (`event_type`, `schema_version`, `event_id`, `event_ts`, `agent` from `SCION_AGENT_NAME`, `template` from `SCION_TEMPLATE`, plus the given fields) to `http://127.0.0.1:${SCION_OTEL_HTTP_PORT:-4318}/v1/logs` with a 0.5-second timeout; it always exits 0 and prints nothing.
  - `factory-shims/git`: finds the real `git` as the first one on `PATH` outside the shims directory. It blocks with exit code 13 and the stderr line `factory-gate: blocked: <reason>`:
    - `push` with `--force`, `-f`, `--force-with-lease` or a `+` refspec;
    - `push` whose destination ref is not `factory/issue-<N>` (a bare `git push` uses the current branch);
    - `add` with `-A`, `--all` or `.`;
    - `clean`, and `reset --hard`;
    - `commit` when the staged diff matches a pattern in `secret-patterns.json`.

    Everything else is passed to the real git. Every decision runs `factory-event factory.hook_verdict` with `gate` (`git.<subcommand>`), `verdict` (`allowed` or `blocked`) and `command` (redacted, at most 512 characters).
  - `factory-shims/gcloud`: unless `SCION_TEMPLATE` is `deployer`, blocks `run deploy`, `run services update`, `run services update-traffic`, `run jobs deploy`, and any `add-iam-policy-binding`, `remove-iam-policy-binding` or `set-iam-policy`; it writes verdict events the same way.
  - `factory-cd.sh` exports `PATH="$HOME/.local/bin/factory-shims:$PATH"` before it execs the harness.
  - `make-factory-harness-configs.py` copies the shims, `factory-event` and `secret-patterns.json` into each `<harness>-factory` config home, with mode `0o755` on executables.
  - `fleet_hooks.WORKFLOW_HOOK_SLUG = "factory-workflow"` and `fleet_hooks.render_workflow_hook(spec: dict) -> str`: the baseline owner-editable workflow hook, rendered from `config/fleet/workflow-hook.sh.tmpl`. It is a project pre-start hook script that writes `.git/hooks/pre-commit` into the agent's clone at `/workspace`; that pre-commit hook runs `stack.test_command` and, when present, `stack.lint_command` from `${FACTORY_SUBDIR}`, blocks the commit on failure, and calls `factory-event factory.hook_verdict` with `gate` `workflow.pre-commit` and `verdict` `allowed` or `blocked`.

- [ ] **Step 1: Write the failing tests** (each runs the shims with a fake "real" `git`/`gcloud` earlier in a temp `PATH` that logs its args, and a local `http.server` capturing `/v1/logs`)

```python
def test_git_shim_blocks_force_push_and_default_branch_push(gate_env):
    for args in (["push", "--force", "origin", "factory/issue-1"], ["push", "origin", "main"],
                 ["push", "origin", "HEAD:main"], ["push", "origin", "+factory/issue-1"]):
        r = run_shim(gate_env, "git", args)
        assert r.returncode == 13 and "factory-gate: blocked" in r.stderr
    assert gate_env.real_calls("git") == []

def test_git_shim_allows_issue_branch_push(gate_env):
    assert run_shim(gate_env, "git", ["push", "origin", "factory/issue-42"]).returncode == 0
    assert gate_env.real_calls("git") == [["push", "origin", "factory/issue-42"]]

def test_git_shim_blocks_broad_add_clean_and_hard_reset(gate_env): ...
def test_git_shim_blocks_commit_with_seeded_secret(gate_env): ...
def test_gcloud_shim_blocks_deploy_for_non_deployer_and_allows_deployer(gate_env): ...

def test_factory_event_posts_otlp_json(otlp_server, gate_env):
    run_shim(gate_env, "factory-event", ["factory.escalation", '{"escalation_id": "e1"}'])
    body = otlp_server.last_json()
    record = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert json.loads(record["body"]["stringValue"])["event_type"] == "factory.escalation"

def test_factory_event_never_blocks_when_receiver_down(gate_env):
    t0 = time.monotonic()
    r = run_shim(gate_env, "factory-event", ["factory.hook_verdict", "{}"], port=1)
    assert r.returncode == 0 and r.stdout == "" and time.monotonic() - t0 < 2

def test_secret_patterns_file_matches_redact_module():
    assert json.loads(PATTERNS_FILE.read_text()) == redact.PATTERNS

def test_factory_cd_exports_shims_first_in_path():
    assert 'export PATH="$HOME/.local/bin/factory-shims:$PATH"' in FACTORY_CD.read_text()

def test_harness_config_packaging_includes_executable_shims(tmp_path): ...

def test_workflow_hook_runs_test_and_lint_commands_and_emits_verdicts(tmp_path):
    script = fleet_hooks.render_workflow_hook({"stack": {"test_command": "go test ./...",
                                                         "lint_command": "golangci-lint run"}})
    assert ".git/hooks/pre-commit" in script and "go test ./..." in script and "golangci-lint run" in script
    assert "factory-event factory.hook_verdict" in script and "workflow.pre-commit" in script
    path = tmp_path / "hook.sh"; path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
```

The three remaining `...` tests: `add -A`, `add .`, `clean -fd` and `reset --hard` exit 13; a staged file containing `ghp_` plus 36 letters makes `commit` exit 13; `gcloud run deploy svc` exits 13 with `SCION_TEMPLATE=backend` and 0 with `SCION_TEMPLATE=deployer`. The packaging test runs the script's local build path against a temp `SCION_HOME` and checks `os.stat(...).st_mode & 0o111` on the copied shims.

- [ ] **Step 2: Run** `pytest tests/test_factory_gates.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the shims (bash, `set -euo pipefail`), `factory-event` (python3), the workflow-hook template and renderer, generate `secret-patterns.json` from `redact.PATTERNS`, and the packaging change.
- [ ] **Step 4: Run** `pytest tests/test_factory_gates.py tests/test_monorepo_factories.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): platform safety gates, the baseline workflow hook and the event helper"`

---

### Task 10: One PR per issue — protocol templates and fleet kits

**Files:**
- Modify: `templates/lead/system-prompt.md`, `templates/_shared/agents.md`, `templates/_shared/AGENTS.md` (kept identical to `agents.md`), `templates/deployer/system-prompt.md`, `templates/ui-craftsman/system-prompt.md`
- Create: `templates/evaluator/system-prompt.md`, `templates/evaluator/scion-agent.yaml`
- Modify: `scripts/role_library.py` (role `evaluator` on the build team; `ui-craftsman` and `factory-analyst` become build roles), `scripts/scion_factory.py` (compile without an Eval project when `project.eval_slug` is absent; fleet kits select the `<harness>-factory` harness configs), `config/factory.schema.json` (`project.eval_slug`, `team.eval_roles`, `vcs.eval_branch` optional)
- Test: `tests/test_role_prompts.py`, `tests/test_fleet_kit.py` (new)

**Interfaces:**
- Consumes: `factory-event` (Task 9), the CLI flag `scion start --branch` (Scion upstream).
- Produces, as exact phrases the tests check:
  - **Lead:** `factory/issue-<N>`; `scion start issue-<N>-<role> -t <role> --branch factory/issue-<N>`; `Closes #<N>`; `tagged revision with no traffic`; `issue-<N>-evaluator`; `issue-<N>-ui-craftsman`; `An owner merges the PR`; `scion stop issue-<N>-` and `scion delete issue-<N>-`; `Start the deployer only when a deploy is needed, and stop it when it is idle`; `evals-change` (the label for PRs that touch `evals/`); `for each owner in ${FACTORY_OWNERS}`; `factory-event factory.escalation`; `factory-event factory.escalation_resolved`.
  - **Shared protocol:** `Push only your issue branch factory/issue-<N>`; `Never push to the default branch`; `gh pr create`. The commit-slot rules for one shared checkout are removed, because each agent has its own clone.
  - **Deployer:** `--tag issue-<N> --no-traffic` for previews and `--to-latest` for the rollout after merge.
  - **Evaluator:** runs the issue's acceptance checks against the preview URL, posts results with `gh pr comment`, and writes `factory-event factory.eval_result` with `issue`, `target`, `check`, `status`.
  - `config/factory.schema.json`: optional top-level `fleet` (boolean, default `false`).

- [ ] **Step 1: Write the failing tests**

```python
def test_lead_prompt_describes_pr_per_issue_flow():
    text = read("templates/lead/system-prompt.md")
    for phrase in LEAD_PHRASES:            # the list above, verbatim
        assert phrase in text, phrase

def test_shared_protocol_forbids_default_branch_push_and_uses_issue_branches():
    text = read("templates/_shared/agents.md")
    assert "Never push to the default branch" in text and "gh pr create" in text
    assert "commit slot" not in text
    assert read("templates/_shared/AGENTS.md") == text

def test_deployer_prompt_uses_no_traffic_preview_and_rollout(): ...
def test_evaluator_template_reports_on_pr_and_emits_eval_result(): ...

def test_compile_without_eval_slug_has_no_eval_project_or_eval_templates(tmp_path):
    spec = starter_spec(); del spec["project"]["eval_slug"]; spec["team"]["eval_roles"] = []
    kit = compile_kit(spec, tmp_path)
    assert not (kit / "templates" / "eval-lead").exists() and not (kit / "templates" / "eval-deployer").exists()
    assert "EVAL_PROJECT" not in (kit / "factory.env.example").read_text()

def test_compile_with_eval_slug_still_supported(tmp_path): ...
def test_fleet_kit_templates_select_factory_harness_configs(tmp_path): ...
```

The fleet-kit test compiles with `fleet: true` and asserts every rendered `scion-agent.yaml` names a `<harness>-factory` harness config. `test_compile_with_eval_slug_still_supported` compiles the unchanged starter spec and asserts the kit still has the `eval-lead` template and `EVAL_PROJECT` in `factory.env.example`. The deployer and evaluator tests assert the phrases listed above.

- [ ] **Step 2: Run** `pytest tests/test_role_prompts.py tests/test_fleet_kit.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the template rewrites, the evaluator role, the schema change and the compiler change. Existing prompt tests that check removed shared-checkout wording are updated to the new wording, never deleted. Where one asserted a safety rule (no `git add -A`, no force push), it now asserts the equivalent rule in the new text.
- [ ] **Step 4: Run** `pytest tests -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): one PR per issue with per-issue agents and an evaluator role"`

---

### Task 11: Warehouse landing zone

**Files:**
- Create: `scripts/setup-warehouse.sh`
- Test: `tests/test_setup_warehouse.py`

**Interfaces:**
- Consumes: the platform env file (`PLATFORM_ENV`, default `config/platform.env`): `HUB_PROJECT`, `WAREHOUSE_DATASET`, `WAREHOUSE_LOCATION`, optional `BILLING_EXPORT_DATASET`.
- Produces, idempotently:
  - enable `bigquery.googleapis.com` and `logging.googleapis.com`;
  - `bq mk --dataset --location=<loc> <host>:<dataset>` when missing;
  - Log Router sink `scion-factory-events` to `bigquery.googleapis.com/projects/<host>/datasets/<dataset>` with `--use-partitioned-tables` and the filter `logName="projects/<host>/logs/scion-analytics" OR jsonPayload.event_type=~"^factory\."` (created, or updated when the filter differs);
  - `roles/bigquery.dataEditor` on the dataset for the sink's writer identity;
  - bucket `gs://<host>-factory-transcripts` with uniform bucket-level access and public access prevention;
  - a billing-export check: when `BILLING_EXPORT_DATASET` is unset or missing, print `billing export: NOT ENABLED` with the console steps, and exit 0.

- [ ] **Step 1: Write the failing tests**

```python
def test_warehouse_dry_run_plans_dataset_sink_bucket(tmp_path):
    out = run_warehouse(tmp_path, DRY_RUN="1")
    assert "bq mk --dataset --location=US host-proj:factory_warehouse" in out
    assert "logging sinks create scion-factory-events" in out and "--use-partitioned-tables" in out
    assert 'logName="projects/host-proj/logs/scion-analytics" OR jsonPayload.event_type=~"^factory\\."' in out
    assert "gs://host-proj-factory-transcripts" in out

def test_warehouse_grants_sink_writer_on_dataset(tmp_path): ...
def test_warehouse_reports_missing_billing_export_without_failing(tmp_path): ...
```

The fake `gcloud` returns `serviceAccount:sink-writer@example.iam.gserviceaccount.com` for `sinks describe --format=value(writerIdentity)`; assert the `bq add-iam-policy-binding --member=serviceAccount:sink-writer@... --role=roles/bigquery.dataEditor host-proj:factory_warehouse` call, and for the billing test, return code 0 with `billing export: NOT ENABLED` in the output.

- [ ] **Step 2: Run** `pytest tests/test_setup_warehouse.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the script in the style of `setup-telemetry.sh` (`run` helper honoring `DRY_RUN`, existence checks before each create).
- [ ] **Step 4: Run** `pytest tests/test_setup_warehouse.py -q && bash -n scripts/setup-warehouse.sh`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): BigQuery raw landing zone, event sink and transcript bucket"`

---

### Task 12: Fleet setup

**Files:**
- Create: `scripts/fleet_onboard.py` (fleet setup part)
- Modify: `scripts/scion_factory.py` (subcommand `fleet-setup`)
- Test: `tests/test_fleet_setup.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 8 and 11.
- Produces:
  - `PhaseResult` dataclass (`phase: str`, `ok: bool`, `detail: dict`); `Report` dataclass (`ok: bool`, `apply: bool`, `phases: list[PhaseResult]`, method `to_json() -> str`).
  - `run_cmd(argv: list[str], *, apply: bool, input_text: str | None = None) -> subprocess.CompletedProcess`: in a dry run, prints `+ <argv>` and returns a success result without running anything.
  - `derive_owner_permissions(admin_permissions: list[str]) -> list[str]`: sorted, without `project.create`, `project.manage`, `harness_config.create`, `harness_config.update`, `harness_config.delete`.
  - `fleet_setup(platform: PlatformConfig, client, store, apply: bool, retire_legacy: list[str], runner=run_cmd) -> Report` with phases in this order:
    1. `legacy`: refuses (`ok=False`, nothing else runs) when the env-driven CronJobs `scion-factory-sync` or `scion-factory-watchdog` exist and `retire_legacy` is empty. Otherwise, for each legacy slug, stop and delete its agents and delete its Scion project; then delete those CronJobs and the KSAs `scion-worker` and `scion-deployer`, and remove the Workload Identity annotation from the `default` KSA.
    2. `control_identity`: ensure the control GSA in the host project, annotate KSA `scion-factory-control` with it, and grant it `roles/datastore.user`, `roles/logging.viewer` and `roles/logging.logWriter` on the host project.
    3. `owner_role`: ensure the Hub role `factory-owner` with `derive_owner_permissions` of the built-in project admin role.
    4. `hub_env`: `PUT /api/v1/env/<KEY>` with `scope: hub` and `injectionMode: always` for each `reserved_env` pair.
    5. `policy`: `kubectl apply` of `render_policy` and of `render_params(registry.active())`.
    6. `control_plane`: `install-k8s-control-plane.py --fleet` (`--apply` when applying).
    7. `warehouse`: `scripts/setup-warehouse.sh` with `DRY_RUN` matching the run.
  - CLI: `scion-factory fleet-setup --platform <platform.env> [--retire-legacy <slug,slug>] [--apply] [--json]`.

- [ ] **Step 1: Write the failing tests** (fake hub, `memory_store`, and a recording runner whose `kubectl get cronjob` answer is configurable)

```python
def test_fleet_setup_dry_run_changes_nothing(fake_hub, memory_store):
    rep = fleet_onboard.fleet_setup(platform(), client(fake_hub), memory_store, apply=False,
                                    retire_legacy=["k8s-version"], runner=recorder(legacy=True))
    assert not [r for r in fake_hub.requests if r["method"] in ("POST", "PUT", "PATCH", "DELETE")]

def test_fleet_setup_refuses_when_legacy_cronjobs_exist_without_retire_flag(fake_hub, memory_store):
    rep = fleet_onboard.fleet_setup(platform(), client(fake_hub), memory_store, apply=True,
                                    retire_legacy=[], runner=recorder(legacy=True))
    assert not rep.ok and [p.phase for p in rep.phases] == ["legacy"]

def test_fleet_setup_retires_legacy_projects_then_installs_policy_and_control_plane(fake_hub, memory_store): ...

def test_derive_owner_permissions_drops_manage_create_and_harness_config():
    perms = ["project.read", "project.manage", "project.create", "template.write", "harness_config.update"]
    assert fleet_onboard.derive_owner_permissions(perms) == ["project.read", "template.write"]

def test_hub_env_sets_each_reserved_value(fake_hub, memory_store): ...
```

In the retire test, assert the phase order matches the list above, `DELETE /api/v1/projects/<id>` for both legacy slugs, `kubectl delete cronjob scion-factory-sync scion-factory-watchdog`, and that the policy apply comes after the legacy deletes. The hub-env test asserts six `PUT /api/v1/env/<KEY>` requests with the reserved values.

- [ ] **Step 2: Run** `pytest tests/test_fleet_setup.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `fleet_setup` and the CLI wiring. Before writing the `owner_role` phase, read the role format from the live Hub (`GET /api/v1/admin/roles/export`) during Task 16 step 1 and match the fake-hub fixture to it.
- [ ] **Step 4: Run** `pytest tests/test_fleet_setup.py tests/test_scion_factory.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): fleet-setup with clean legacy retirement, owner role and policy"`

---

### Task 13: Onboard

**Files:**
- Modify: `scripts/fleet_onboard.py` (onboarding part), `scripts/scion_factory.py` (subcommand `onboard`)
- Test: `tests/test_fleet_onboard.py`

**Interfaces:**
- Consumes: Tasks 1–6, 9 (`render_workflow_hook`), 12 (`run_cmd`, `Report`).
- Produces:
  - `codeowners_lines(subdir: str, owners: list[Owner]) -> list[str]`: `["/<subdir>/ @a @b", "/<subdir>/evals/ @a @b"]`; an empty subdir gives `/` and `/evals/`.
  - `merge_codeowners(existing: str, lines: list[str]) -> str`: appends the lines that are missing and keeps every other line.
  - `protection_payload(required_checks: list[str]) -> dict`: `required_status_checks` (`strict: False`, `contexts`) or `None` when the list is empty; `enforce_admins: False`; `required_pull_request_reviews` with `require_code_owner_reviews: True` and `required_approving_review_count: 1`; `restrictions: None`; `allow_force_pushes: False`; `allow_deletions: False`.
  - `run_gh(args: list[str], *, token: str, apply: bool, input_text: str | None = None) -> subprocess.CompletedProcess`, calling `gh api` with `GH_TOKEN` set to the factory's token.
  - `onboard_factory(spec: dict, kit_dir: Path, factory_env: Path, platform: PlatformConfig, owners: list[Owner], client, store, apply: bool, runner=run_cmd, gh=run_gh) -> Report` with phases:
    1. `validate`: slug valid and free in the Hub and the registry; sandbox not used by an active factory; the kit was compiled with `fleet: true` (every template names a `<harness>-factory` harness config).
    2. `identities`: `setup-sandbox.sh` with `FLEET_MODE=1`, the Task 1 names, and `MODEL_PROJECT` and `TELEMETRY_PROJECT` set to the host.
    3. `project`: one Scion project; register the worker GSA (and the deployer GSA when deploy is `service`); set identity mode `assign` with the worker GSA.
    4. `artifacts`: `push-templates.py --create-only`, then `create_missing` for the workflow hook (`hook:factory-workflow` from `render_workflow_hook(spec)`), env (including `FACTORY_OWNERS` as comma-separated emails) and the schedule `<slug>-lead-heartbeat`, with `baseline_keys` from an existing registry record. The weekly analyst schedule is created by the Stage 2 plan, which builds what the analyst reads.
    5. `repo`: a setup PR on branch `factory/onboard` updating CODEOWNERS with `merge_codeowners`, and branch protection with `protection_payload`. When the token cannot set protection (HTTP 403), the phase stays `ok` and `detail.missing_protection` holds the payload for an owner to apply.
    6. `owners`: IAP access on the Hub service for each email, a Hub user, and membership with role `factory-owner`.
    7. `baseline`: `read_artifacts`, `hash_artifacts` and `put_snapshot` (first onboarding only).
    8. `registry`: `put` the record (`state: active`).
    9. `policy`: `render_params(registry.active())` applied with `kubectl apply`.
    10. `lead`: `ensure_agent_started` for the lead (template `lead`, `harnessAuth` `vertex-ai`).
  - CLI: `scion-factory onboard <factory.yaml> --kit <dir> --env <factory.env> --platform <platform.env> --owner <email>=<login> [--owner ...] [--apply] [--json]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_onboard_dry_run_makes_no_writes(fake_hub, memory_store, tmp_path): ...
def test_onboard_refuses_taken_slug_and_registered_sandbox(fake_hub, memory_store, tmp_path): ...

def test_onboard_apply_phases_in_order_and_registry_record(fake_hub, memory_store, tmp_path):
    rep = onboard(fake_hub, memory_store, tmp_path, apply=True)
    assert [p.phase for p in rep.phases] == ["validate", "identities", "project", "artifacts", "repo",
                                             "owners", "baseline", "registry", "policy", "lead"]
    rec = FleetRegistry(memory_store).get("shop")
    assert rec["identities"]["worker_ksa"] == "shop-worker" and rec["baseline"]["hashes"]
    assert rec["owners"] == [{"email": "ana@example.com", "github": "ana-gh"}]

def test_rerun_does_not_update_or_delete_artifacts(fake_hub, memory_store, tmp_path):
    onboard(fake_hub, memory_store, tmp_path, apply=True); fake_hub.requests.clear()
    rep = onboard(fake_hub, memory_store, tmp_path, apply=True, allow_existing=True)
    assert not [r for r in fake_hub.requests if r["method"] in ("PUT", "PATCH", "DELETE")
                and "/settings" not in r["path"]]
    assert phase(rep, "artifacts").detail["left_alone"]

def test_rerun_does_not_recreate_owner_deleted_artifact(fake_hub, memory_store, tmp_path):
    onboard(fake_hub, memory_store, tmp_path, apply=True)
    fake_hub.delete_env("p-shop", "FACTORY_OWNERS")
    rep = onboard(fake_hub, memory_store, tmp_path, apply=True, allow_existing=True)
    assert "env:FACTORY_OWNERS" in phase(rep, "artifacts").detail["skipped_deleted"]

def test_codeowners_merge_appends_second_factory_on_monorepo():
    first = "\n".join(fleet_onboard.codeowners_lines("src/shop", [Owner("a@example.com", "ana")])) + "\n"
    merged = fleet_onboard.merge_codeowners(first, fleet_onboard.codeowners_lines("src/blog", [Owner("b@example.com", "bo")]))
    assert "/src/shop/ @ana" in merged and "/src/blog/evals/ @bo" in merged

def test_onboard_rerenders_params_from_full_registry(fake_hub, memory_store, tmp_path): ...
def test_onboard_reports_missing_branch_protection_rights(fake_hub, memory_store, tmp_path): ...
```

`allow_existing=True` models a re-run: the `validate` phase accepts a slug whose registry record has the same sandbox and repo, and every later phase runs in create-only mode. The remaining `...` tests check:
- the dry run makes no Hub writes and only dry-run runner calls;
- `ok=False` with `validate` as the only phase, for a slug already in the registry, for a sandbox owned by another active factory, and for a kit compiled without `fleet: true`;
- after a second onboarding, the applied params contain both factories' `ksa.` keys;
- when the `gh` fake returns 403 on `PUT .../protection`, the phase reports `detail.missing_protection` with `require_code_owner_reviews: True`.

- [ ] **Step 2: Run** `pytest tests/test_fleet_onboard.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `onboard_factory`, the helpers and the CLI wiring. Add `allow_existing: bool = False` to `onboard_factory`; the CLI sets it when the slug is already registered to the same sandbox and repo.
- [ ] **Step 4: Run** `pytest tests/test_fleet_onboard.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): onboard a factory with create-only artifacts, owners and repo protection"`

---

### Task 14: Retire, and provision refusal

**Files:**
- Modify: `scripts/fleet_onboard.py` (retire part), `scripts/provision_factory.py`, `scripts/scion_factory.py` (subcommand `retire`)
- Test: `tests/test_fleet_onboard.py`, `tests/test_provision_factory.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 12, 13.
- Produces:
  - `retire_factory(slug: str, platform: PlatformConfig, client, store, apply: bool, runner=run_cmd) -> Report` with phases:
    1. `owners`: remove memberships; remove IAP access only for emails that own no other active factory.
    2. `agents`: stop and delete every agent.
    3. `schedules`: pause each schedule and record its name with `set_platform_paused`.
    4. `identities`: delete the factory's KSAs and their Workload Identity bindings.
    5. `registry`: `mark_retired`.
    6. `policy`: apply `render_params(registry.active())`.

    It never touches the repo or the warehouse.
  - `provision_factory(...)`: when `FLEET_REGISTRY` is set in its env, or a fleet registry document exists in the configured Firestore, it returns `{"ok": False, "error": "this platform runs in fleet mode: use scion-factory onboard"}` before any change.
  - CLI: `scion-factory retire <slug> --platform <platform.env> [--apply] [--json]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_retire_removes_access_agents_ksas_and_params(fake_hub, memory_store, tmp_path): ...
def test_retire_keeps_iap_for_owner_of_another_factory(fake_hub, memory_store, tmp_path): ...
def test_retire_dry_run_changes_nothing(fake_hub, memory_store, tmp_path): ...

def test_provision_refuses_on_fleet_platform(fake_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_REGISTRY", "firestore")
    res = provision_factory.provision_factory(starter_spec(), tmp_path, write_gke_factory_env(tmp_path, fake_hub.url), apply=True)
    assert res == {"ok": False, "error": "this platform runs in fleet mode: use scion-factory onboard"}
```

The retire tests onboard two factories first, then assert:
- the member `DELETE` for the retired factory, the agent stop and delete requests, `kubectl delete serviceaccount shop-worker shop-deployer`, and the absence of `ksa.p-shop` in the last applied params;
- no IAP removal for an owner who also owns the other factory;
- no writes at all in a dry run.

- [ ] **Step 2: Run** `pytest tests/test_fleet_onboard.py tests/test_provision_factory.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `retire_factory`, the provision guard and the CLI wiring.
- [ ] **Step 4: Run** `pytest tests -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(fleet): retire a factory cleanly and block legacy provision on fleet platforms"`

---

### Task 15: Docs and repo rules

**Files:**
- Modify: `AGENTS.md` (rule 3, repository structure rows for the new scripts and docs), `README.md` (a "Fleet mode" section linking the runbook and owner guide), `docs/architecture.md` (fleet mode section), `docs/gke-setup-runbook.md` (pointer to the fleet runbook)
- Create: `docs/fleet-runbook.md` (fleet-setup, onboard, retire, checks, rollback limits), `docs/project-owner-guide.md` (what owners get; safety gates and workflow hooks; owner-editable versus platform-owned; live edits are recorded; what a rejected start means; how to escalate)
- Test: `tests/test_docs_contract.py`

**Interfaces:**
- Produces: AGENTS.md rule 3, verbatim:

  > **Serverless First, No Spanner in the Factory:** Firestore holds the control plane's own state through `scripts/factory_store.py` (fleet registry, watchdog bookmarks). Agent status, telemetry and evaluation data go to BigQuery through Cloud Logging. Never add Spanner (or another provisioned database) to the factory itself; products may use one only when the interview records a requirement Firestore or BigQuery can't meet.

- [ ] **Step 1: Write the failing tests**

```python
def test_agents_md_rule_3_moves_status_and_evals_to_bigquery():
    text = (ROOT / "AGENTS.md").read_text()
    assert "Agent status, telemetry and evaluation data go to BigQuery through Cloud Logging." in text
    assert "(fleet registry, watchdog bookmarks)" in text

def test_readme_links_fleet_runbook_and_owner_guide():
    readme = (ROOT / "README.md").read_text()
    for rel in ("docs/fleet-runbook.md", "docs/project-owner-guide.md"):
        assert rel in readme and (ROOT / rel).exists()

def test_owner_guide_lists_editable_and_platform_owned_items():
    guide = (ROOT / "docs/project-owner-guide.md").read_text()
    for phrase in ("Owner-editable", "Platform-owned", "rejected start", "factory/issue-<N>"):
        assert phrase in guide
```

- [ ] **Step 2: Run** `pytest tests/test_docs_contract.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Write** the docs. Keep the README's existing contract tests passing (anchors, links, STE100 rules in `test_readme_follows_google_dev_style_and_ste100_without_content_loss`).
- [ ] **Step 4: Run** `pytest tests -q`. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "docs(fleet): fleet runbook, owner guide, and rule 3 for BigQuery"`

---

### Task 16: Prove it on the live GKE platform

This task changes the live shared platform. Real values live only in git-ignored env files.

**Files:**
- Create (git-ignored): `config/platform.env`, `config/factory-a.env`, `config/factory-b.env`
- Modify: `docs/lessons-learned.md` (findings from this run), issue #7 checklist

**Prerequisites the platform admin provides:**
- Two new sandbox projects with billing (`<SANDBOX_A>`, `<SANDBOX_B>`).
- Factory A: the existing monorepo factory's FactorySpec (`infrastructure.deploy: none`), re-onboarded. Factory B: a small repo with one Cloud Run HTTP service and a FactorySpec with `infrastructure.deploy: service`.
- One fine-grained GitHub token per factory repo (contents, pull requests, administration for branch protection).
- Claude models enabled in Model Garden in the host project, since every factory's model calls now bill there.
- Cloud Billing export to BigQuery enabled in the console.

- [ ] **Step 1: Snapshot today's platform.** Save `kubectl get pods,cronjobs,sa -n <namespace> -o yaml`, the Hub project list and `GET /api/v1/admin/roles/export` to the scratch directory. Expected: the legacy factory and env-driven CronJobs are present.
- [ ] **Step 2: Stop and confirm with the platform admin** before anything changes: the next step deletes the legacy Build and Eval projects and their agents (clean start, decision 4).
- [ ] **Step 3: Publish harness configs.** `python3 scripts/make-factory-harness-configs.py --via-api --apply` with the platform env. Expected: every `<harness>-factory` config lists `factory-shims/git`, `factory-shims/gcloud` and `factory-event` in its home.
- [ ] **Step 4: Fleet setup.** `./scripts/scion-factory fleet-setup --platform config/platform.env --retire-legacy <legacy-build>,<legacy-eval>` (review the dry run), then the same with `--apply`. Expected: all phases `ok`; `kubectl get validatingadmissionpolicy scion-fleet-agents` exists; `kubectl get cronjobs -n <namespace>` shows only `scion-fleet-sync`, `scion-fleet-watchdog`, `scion-fleet-drift`.
- [ ] **Step 5: Prove the policy with server-side dry runs.** Render six pod manifests labeled like Scion agent pods (copied from a live pod's labels and volumes): valid factory pod, foreign KSA, `scion-factory-control` KSA, foreign image, changed `GOOGLE_CLOUD_PROJECT`, foreign `subPath`, plus one with an unregistered `scion.project_id`. Run `kubectl apply --dry-run=server -f <file>` for each. Expected before onboarding: all denied (`registered`). After Step 6: the valid pod is admitted; each other one is denied with its rule name in the message.
- [ ] **Step 6: Onboard factory A** (dry run, then `--apply`), with test owner `fleet-owner-a@<host>` (a service account created here, mapped to the platform admin's GitHub login). Expected: all phases `ok`; registry record `active`; lead pod running as `<slug-a>-worker`; `kubectl get pod -o jsonpath` shows `GOOGLE_CLOUD_PROJECT=<host>` and the telemetry env.
- [ ] **Step 7: Onboard factory B**, with owner `fleet-owner-b@<host>`. Before and after, diff factory A's registry record, KSAs and the CronJobs. Expected: no difference for A, apart from the params ConfigMap gaining B's keys (done-when 1).
- [ ] **Step 8: Isolation tests as owner A** (Hub client impersonating `fleet-owner-a`). Expected:
  - listing projects does not return B, and messaging B's lead fails with 403 or 404;
  - registering any GSA and creating a project both fail;
  - re-running Step 5 with A's labels and B's KSA, image, env or paths is denied (done-when 2).

  If B is listed, stop and report it to the platform admin before going on.
- [ ] **Step 9: Live edits survive.** As owner A: edit the lead template's prompt, the workflow hook, and pause the heartbeat. Re-run `onboard` for A with `--apply`, then wait 16 minutes. Expected:
  - all three edits are still in place;
  - `gcloud logging read 'jsonPayload.event_type="factory.drift"' --project=<host>` shows three entries for A (done-when 3).
- [ ] **Step 10: One issue end to end in factory B.** Open an issue labeled with B's slug ("Add a `/health` endpoint that returns `ok`") and message the lead `GO`. Expected, in order:
  1. branch `factory/issue-<N>`;
  2. a PR with `Closes #<N>`;
  3. a PASS/FAIL review comment;
  4. a Cloud Run revision tagged `issue-<N>` with 0% traffic;
  5. an evaluator comment with results and `factory.eval_result` entries in Cloud Logging.

  Then the platform admin merges as owner. After the merge: the rollout to live traffic; `issue-<N>-*` agents deleted; only B's lead in `kubectl get pods -l scion.project=<slug-b>` (done-when 4).
- [ ] **Step 11: Billing and landing.**
  - Every agent pod's `GOOGLE_CLOUD_PROJECT` is the host.
  - `gcloud projects get-iam-policy <SANDBOX_A>` grants no `roles/aiplatform.user` to A's worker.
  - `bq query --use_legacy_sql=false 'SELECT table_name FROM factory_warehouse.INFORMATION_SCHEMA.TABLES'` lists the sink tables, and they hold rows for `factory.agent`, `factory.drift`, `factory.hook_verdict`, `factory.eval_result` and `scion-analytics` usage (done-when 5).

  If `scion-analytics` has no rows, record it as a Stage 2 blocker (the deployed `sciontool` may predate it).
- [ ] **Step 12: Retire factory B** (dry run, then `--apply`). Expected:
  - owner B's membership and IAP access are gone;
  - B's KSAs are gone, and the params lack B's keys;
  - stopping and starting A's lead through the Hub API still admits it (done-when 6).
- [ ] **Step 13: Run** `pytest tests -q` and every new script with `DRY_RUN=1` or `--dry-run`. Expected: all pass (done-when 7).
- [ ] **Step 14: Record and hand over.** Add the run's findings to `docs/lessons-learned.md`, tick issue #7's Stage 1 items, run the secret scan from `AGENTS.md`, commit (`docs(fleet): record the Stage 1 live verification`) and push. Only then open the PR from `feat/multi-project-factory` to `main`.
