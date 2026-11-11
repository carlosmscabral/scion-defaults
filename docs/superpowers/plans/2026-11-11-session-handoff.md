# Session Handoff — GKE Factory Hillclimb (`duboc/scion-defaults`)

**Date:** 2026-11-11
**Source Environment:** `jetski-cli` → **Target Environment:** `jetski hub`
**Active Branch:** `feat/gke-factory-hillclimb` (branched from `main` at `2fc4bb1`)
**Head Before Handoff Commit:** `e051797`
**Test Baseline (Verified on `e051797`):** `uv run --with pytest --with pyyaml --with jsonschema pytest tests -q` → **`404 passed in 107.51s`** (zero skipped, secret scan clean)

---

## 1. Objective & Repository Context

We are empirically improving core `duboc/scion-defaults` on GKE Autopilot + Cloud Run HA before layering multi-project fleet work back on top:

- **Spec:** [`docs/superpowers/specs/2026-11-10-gke-factory-hillclimb-design.md`](../specs/2026-11-10-gke-factory-hillclimb-design.md)
- **Implementation Plan (Tasks 1–7):** [`docs/superpowers/plans/2026-11-10-gke-factory-hillclimb.md`](2026-11-10-gke-factory-hillclimb.md)
- **Target Workload for Live Runs:** `https://github.com/duboc/bananacast` on isolated hillclimb branches (`factory/gke-hc<k>`); `duboc/bananacast` `main` and `factory/main` are never modified.

### Why This Work Exists (Forensic Findings from Backup Logs)
1. **Direct IAP `401` flood (~40k error lines/pod):** `sciontool init` (PID 1) moved its bootstrap credential to `/home/scion/.scion/transport-token` while `factory-cd.sh` only refreshed `~/.scion/iap-access-token` after `tmux` launched.
2. **Telemetry boot failure:** GKE Workload Identity ADC requires `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` alongside `SCION_GCP_PROJECT_ID`.
3. **Unrecovered Autopilot evictions:** Evicted pods entered `phase: halted` (`exit_reason: evicted`), which `factory-watchdog.py` did not recover or retire.
4. **93%–99.9% idle LLM spend + split-brain evals:** `*/15` `lead` crons busted prompt cache TTL and burned up to `203M–639M` tokens while idle; separate `<slug>-eval` projects blocked cross-project `scion message` (`404`) and needed `*/5` polling (`933M` tokens on VM) or did nothing on GKE.

---

## 2. Decisions Taken

### Product & Architecture Decisions
- **Ephemeral full-lifecycle runs (`gke-up` → wave + idle soak → `gke-down --delete-project`):** Every live hillclimb run creates a brand-new GCP project (`scion-gke-hc<k>-<6hex>` in `us-east1`, `SQL_AVAILABILITY_TYPE=ZONAL`, `VERTEX_LOCATION=global`), runs end-to-end, archives pod logs/transcripts/scorecard locally (`0700`), and deletes the GCP project + closes GitHub test issues/branches so cold-start GKE setup is tested every pass and `$0` infra lingers.
- **Harness & model pin:** Antigravity only (`antigravity-factory` on GKE) with `Gemini 4 Argon` and `GOOGLE_CLOUD_LOCATION=global` across all roles (`lead`, `backend`, `frontend`, `reviewer`, `security-auditor`, `evaluator`).
- **Single Hub project + delta orchestration:** `project.eval_slug` is optional (omitted here); `evaluator` runs in-project on demand. `*/15` LLM crons are replaced by `scripts/factory_delta.py` (`CronJob` `scion-factory-delta` `*/2 * * * *`), which wakes `lead` only on actionable GitHub/child-pod deltas, rotates `lead` at `25` turns (when no child pods are running) using `/scion-volumes/scratchpad/STATE.json`, and stops `lead` + idle pods after `600s` idle (`lead_sleeping: true`, `$0` idle spend).
- **Direct peer handoff:** Writers implement on `factory/issue-<N>`, push directly to `origin/factory/issue-<N>`, and message `reviewer` directly (`reviewer` bounces once directly to the writer on first `FAIL`, waking `lead` only on `PASS` or second `FAIL`). Unused specialists are never pre-started.

### Engineering Rulings (`R1`–`R6`) & Pre-Live Review Resolutions
- **R1 (`bananacast-gke.factory.yaml`):** Dropped unused `ALWAYS_BUILD` defaults (`researcher`, `doc-writer`, `investigator`) explicitly via `team.dropped_defaults` with reasons so `normalize_team` preserves `"Gemini 4 Argon"` on all 6 active roles and compiled `factory.yaml` validates cleanly against `config/factory.schema.json`.
- **R2 (Whole-branch gate before Task 7):** Completed full Tasks 1–6 code review (`094e4e2`) + fix wave (`62a431f`, `0c914ce`, `e051797`) before running live GCP cold-start in Task 7.
- **R3 (Worktree path discipline):** Every edit uses worktree paths and every shell command explicitly `cd`s into the active worktree checkout.
- **R4 (`setup-gke-hub.sh --write-env`):** Also defaults/merges `SANDBOX_REGION`, `FIRESTORE_PROJECT`, and `DATA_BUCKET` (while preserving existing keys/comments/`0600`) needed by `setup-sandbox.sh` (`set -u`) and `factory_store.py`.
- **R5 (`GOOGLE_CLOUD_LOCATION=global`):** Enforced on compiled `templates/<role>/scion-agent.yaml` outputs rather than adding a non-schema `env:` key to `$defs.role`.
- **R6 (Lossless telemetry across pod stop/auto-sleep):** Because GKE `/home/scion` is pod-local and deleted on `scion stop` / eviction, `10-persist-telemetry` (pre-stop hook) + `factory-cd.sh` (periodic 30s + exit trap) mirror `agent.log` and `transcript_full.jsonl` to `/scion-volumes/scratchpad/.telemetry/<agent_slug>/<pod_hostname>/`, and `gke_down` pulls `.telemetry/` via the Hub shared-dir API, keys archived logs by `<pod_name>`, and deduplicates transcripts per `(agent_slug, rel_path)` keeping largest size so `scorecard.json` is accurate even at `0` running pods.

---

## 3. Files Created & Modified (`2fc4bb1..e051797`)

### Task 1 (`3f8120f`) — Pod Direct IAP & Telemetry IAM
- **Created:** `config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token`, `tests/test_direct_iap_and_telemetry.py`
- **Modified:** `config/harness/home/.local/bin/factory-cd.sh`, `scripts/make-factory-harness-configs.py`, `scripts/setup-sandbox.sh`, `tests/test_setup_sandbox.py`

### Task 2 (`0727453`) — Cold-Start GKE Hub Bootstrap
- **Modified:** `scripts/setup-gke-hub.sh` (`--create-project`, `--folder-id`/`--org-id`/`--billing-account`, auto-cloned `scion-src` when `--scion-repo` omitted, `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` + `SCION_GCP_PROJECT_ID`, atomic `--write-env`), `tests/test_setup_gke_hub.py`

### Task 3 (`196a371`) — Zero-LLM Delta Controller, Eviction Recovery, & K8s Control Plane
- **Created:** `scripts/factory_delta.py`, `tests/test_factory_delta.py`
- **Modified:** `scripts/factory-watchdog.py` (`phase == "halted"` recovery + `lead_sleeping` respect), `scripts/install-k8s-control-plane.py` (`--enable-delta`, `scion-factory-delta` `CronJob`, `scion-factory-github-auth` `Secret`), `tests/test_gke_watchdog_and_sync.py`

### Task 4 (`71ada9f`) — Single-Project Schema, `evaluator` Role, & Delta Prompts
- **Created:** `tests/test_single_project_delta_kit.py`
- **Modified:** `config/factory.schema.json`, `scripts/role_library.py` (`evaluator` role + delta peer rules), `scripts/scion_factory.py` (`has_eval_project`, `wake_mode`, ephemeral `factory/issue-<N>` & delta prompts), `scripts/knowledge_plane.py`, `tests/test_role_library.py`

### Task 5 (`a77100f`) — Single-Project Delta Provisioning & `gke-up` / `gke-down`
- **Created:** `scripts/gke_lifecycle.py`, `tests/test_gke_lifecycle.py`
- **Modified:** `scripts/provision_factory.py`, `scripts/scion_factory.py` (`gke-up` and `gke-down` CLI subcommands), `tests/test_provision_factory.py`

### Task 6 (`45fcfbf`, `094e4e2`) — `bananacast` Preset & Wave Harness
- **Created:** `presets/bananacast-gke.factory.yaml`, `scripts/run_hillclimb_wave.py`, `tests/test_hillclimb_wave_runner.py`

### Pre-Live Fix Wave (`62a431f`, `0c914ce`, `e051797`) — Addressed Review Findings #1–#7
- **Created:** `config/harness/home/.scion/hooks/pre-stop.d/10-persist-telemetry`
- **Modified:** `scripts/setup-gke-hub.sh` (`us-east1` & `USE_OPERATOR_SA=1` default on `--create-project --hub-mode cloudrun`, fail-fast on `serviceAccountTokenCreator` binding after 5 retries, `/token/refresh` Direct IAP guard on `pkg/hub/handlers_agents_core.go`), `config/harness/home/.local/bin/factory-cd.sh` (`SCION_TRANSPORT_TOKEN_FILE` + background `.telemetry/` mirror), `scripts/make-factory-harness-configs.py`, `scripts/factory_delta.py` (90s `DELTA_TMUX_WAIT_SEC` + early `waiting_for_tmux` return without advancing fingerprint), `scripts/factory-watchdog.py` (delegate `halted` delta specialists to `factory_delta.py`), `scripts/scion_factory.py` & `scripts/provision_factory.py` (`/scion-volumes/knowledge-plane/INDEX.md`, digest self-creation, specialist resume fallback), `scripts/gke_lifecycle.py` & `scripts/run_hillclimb_wave.py` (Hub scratchpad `.telemetry` download, `<pod_name>` keying, `auth_lost` metric matching, wave digest seeding, `factory/issue-<N>` branch cleanup, refreshed `down_report["metrics"]`), plus covering tests in `tests/`.

---

## 4. Pending Items & Open Work

1. **Formal scoped re-review of Fix Wave (`094e4e2..e051797`):** Code and tests (`404 passed`) are already committed, and the pre-live fix implementer hit an API quota `429` only when finalizing its markdown report. Run one quick read-only review pass over `git diff 094e4e2..e051797` against all 7 items before starting live GCP runs.
2. **Task 7 — 3 Ephemeral Live E2E Hillclimb Runs (`duboc/bananacast`):**
   - **Prerequisites in Jetski Hub (git-ignored `config/hc1.env`..`hc3.env`):**
     - `gcloud` authenticated with permission to create GCP projects under the active folder/org, link billing (`--billing-account` or single open billing account), create Cloud Run / Cloud SQL / Filestore / GKE Autopilot / Artifact Registry, and delete `scion-gke-hc*` projects.
     - Tools installed in `$PATH`: `gcloud`, `terraform`, `kubectl`, `gh`, `git`, `uv`, `python3`.
     - `GITHUB_TOKEN` (PAT with `repo` / contents + issues + PRs for `duboc/bananacast`), `PO_EMAIL`, and optional `FOLDER_ID` / `BILLING_ACCOUNT`.
   - **Run 1 (`hc1`):** `python3 scripts/run_hillclimb_wave.py --wave 1 --env config/hc1.env --apply --json` → inspect archived `scorecard.json` & logs → refine code/prompts + unit tests.
   - **Run 2 (`hc2`):** `python3 scripts/run_hillclimb_wave.py --wave 2 --env config/hc2.env --apply --json` → verify `0` IAP/telemetry errors, `$0` idle soak, fewer `lead` turns → refine for Run 3.
   - **Run 3 (`hc3` + drills):** `python3 scripts/run_hillclimb_wave.py --wave 3 --drills --env config/hc3.env --apply --json` (verifies `kubectl delete pod` eviction recovery and `status:po-decision` scale-to-zero sleep + `<=2m` wake on unmarked human PO comment).
3. **Final Documentation & Closure (end of Task 7):**
   - Create `docs/gke-hillclimb-report.md` (anonymized IDs only per `AGENTS.md` Rule 1) with Run 1 / Run 2 / Run 3 scorecard vs. previous baselines.
   - Add `L34+` lessons to `docs/lessons-learned.md`; update `docs/gke-setup-runbook.md`, `README.md` (must satisfy `tests/test_docs_contract.py` ASD-STE100 rules), `AGENTS.md`, and add `test_docs_cover_gke_hillclimb_delta_orchestration_and_l34` in `tests/test_docs_contract.py`.
   - Run final whole-branch code review (`2fc4bb1..HEAD`) before merging to `main`.
4. **Deferred Non-Blocking Minors (for final review triage):**
   - `scripts/make-factory-harness-configs.py`: redundant local `import base64` inside `pre_start_iap_bytes()`.
   - `scripts/gke_lifecycle.py`: `_cleanup_github_artifacts` fetches `per_page=100` without pagination.
5. **Sibling Paused Workstream (`feat/multi-project-factory`):**
   - Stage 1 Tasks 1–15 + whole-branch review (`0cceeec..083d660`, `520 passed`) are complete locally in `.worktrees/docs-multi-project-factory` (`origin/feat/multi-project-factory` is still at `0cceeec`). Wait until `feat/gke-factory-hillclimb` finishes Task 7, then rebase/merge and execute Stage 1 Task 16 (`docs/plans/2026-11-06-multi-project-factory-stage1.md`).
   - *(Operator note on `main`: commit `2fc4bb1` sits on `main` / `origin/main` from an earlier Stage 1 wrong-checkout commit; leave untouched or clean up only with explicit operator approval.)*

---

## 5. How to Verify Baseline on Arrival in Jetski Hub

```bash
git checkout feat/gke-factory-hillclimb
uv run --with pytest --with pyyaml --with jsonschema pytest tests -q
# Expected: 404 passed

python3 -m py_compile scripts/*.py
uv run --with pyyaml python3 scripts/run_hillclimb_wave.py --help
rg -i '(ghp_|github_pat_|scion_pat_|AIza|ya29\.|BEGIN PRIVATE KEY)' . --glob '!config/*.env' --glob '!.superpowers/**'
```
