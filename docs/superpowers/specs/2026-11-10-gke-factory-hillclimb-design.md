# GKE Factory Hillclimb — Turnkey Setup, Zero-Error Pods, and Delta-Driven Orchestration

- **Date:** 2026-11-10
- **Branch:** `feat/gke-factory-hillclimb`
- **Target Repository (Live E2E Testbed):** `duboc/bananacast` (on isolated `factory/gke-hc*` branches, cleaned up after each run)
- **Status:** Ready for human approval

---

## 1. Intent and Why

`scion-defaults` proved in earlier runs (`L30`–`L33`) that a multi-agent software factory can ship verified features and security fixes autonomously, and that Scion can run on Cloud Run HA + GKE Autopilot with Workload Identity. However, forensic inspection of our production backups revealed four major operational and cost bottlenecks preventing `scion-defaults` on GKE from being turnkey and economical:

1. **Cold-start GKE setup is brittle and multi-step:** `scripts/setup-gke-hub.sh` requires a pre-existing GCP project, demands a local `--scion-repo` checkout whose Go files it mutates in place, builds unused harness images by default, and prints environment variables that the operator must manually copy into `config/factory.env` before running `provision_factory.py`.
2. **Every GKE agent pod logs tens of thousands of runtime errors:**
   - **PID 1 Direct IAP `401` loop (~40,000 error lines/pod):** `sciontool init` (PID 1) moves its bootstrap credential to `/home/scion/.scion/transport-token`, clears `SCION_TRANSPORT_TOKEN` on re-exec, and runs `StartHeartbeat` + `StartTokenRefresh` reading `/home/scion/.scion/transport-token`. `factory-cd.sh` only refreshed `~/.scion/iap-access-token` after `tmux` launched, leaving PID 1 with an invalid/expired token (`Token refresh failed: status 401: Invalid IAP credentials: empty token` → `Heartbeat failed: hub status writes paused`).
   - **Telemetry startup failure on boot:** Under GKE Workload Identity (ADC, no static SA JSON key file), `sciontool`'s `telemetry.Config.IsCloudConfigured()` requires **both** `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` and `SCION_GCP_PROJECT_ID=<project>`. Missing `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` caused `Failed to start telemetry: cloud telemetry enabled but destination is not configured` in every pod.
   - **Unrecovered GKE Autopilot evictions:** Keeping idle specialist pods running 24/7 led Autopilot node consolidation to evict pods (`phase: halted`, `exit_code: 255`, `exit_reason: evicted`), which `factory-watchdog.py` (`run_api_watchdog`) ignored because it only checked `phase in ("error", "stopped")`.
3. **93%–99.9% of `lead` and `eval-lead` token spend is pure idle polling waste:**
   - On our previous GKE run (Antigravity + Gemini 4 Argon), shipping two issues took **46 minutes, 17 turns, and `236K` uncached input tokens** (`5.7M` cached) — followed by 12 days of `*/15` cron heartbeats that burned **`203.2M` uncached input tokens** (`261.6M` cached) and `1,907` shell commands with zero open issues (**99.88%** idle waste).
   - On the previous `bananacast` VM run, shipping 29/30 issues took **`45.8M` cache-write tokens**, while 13 days waiting on one PO decision burned **`593.4M` cache-write tokens** (**92.8%** idle waste).
   - Because Scion blocks cross-project `scion message` (`404`), a separate `<slug>-eval` project required a `*/5` cron (`3,206` wake-ups → **`933M` total tokens**) on VM, or ran **zero** evaluations on GKE when unscheduled.
4. **Clumsy active-work orchestration:**
   - `lead` spawned unused specialists upfront (`researcher`, `investigator` sat at `0` tasks on GKE while burning pods and logs).
   - On GKE `per-agent` checkouts, `backend` wrote `.patch` files to `/scion-volumes/scratchpad/patches/`, waking `lead` 4 times per issue (`backend → lead → reviewer → lead → backend commit → lead`), while child `COMPLETED` `state-change` notifications arrived alongside `scion message`s and woke `lead` twice.

The goal of this project is to **empirically hillclimb `scion-defaults` across three live end-to-end runs on a brand-new GCP project against `duboc/bananacast`**, fixing setup friction, eliminating pod errors, replacing LLM cron polling with zero-LLM delta orchestration + ephemeral per-issue specialists, and documenting every iteration's measurements and lessons back into `scion-defaults`.

---

## 2. Goals, Non-Goals, and Anti-Goals

### Goals
1. **One-command cold-start GKE setup:** From zero to a running GKE HA Scion factory in a brand-new GCP project (project creation, billing link, API enablement, isolated Scion source build cache, `scion-antigravity` image build, two-pass Cloud Run Direct IAP Terraform, `config/factory.env` writeback, kit compilation, and factory provisioning) with zero manual env copy-paste.
2. **Zero pod auth/telemetry errors & eviction resilience:** `0` Direct IAP `401` / `Heartbeat failed` lines in `agent.log`, `0` `Failed to start telemetry` boot errors, and automatic recovery (or clean retirement) when GKE Autopilot evicts a pod (`phase: halted`).
3. **`$0` idle LLM & pod cost:** Delete the `*/15` LLM heartbeat schedule and `*/5` `eval-sweep` schedule; replace them with a Python delta controller in the K8s control plane that wakes `lead` **only** on actionable GitHub/Hub state changes and stops `lead` + specialist pods when the factory is idle.
4. **Lean single-project orchestration:** Run `evaluator` on-demand inside the single factory Hub project (retiring the default `<slug>-eval` split-brain project), start only the specialist roles an active issue needs on `factory/issue-<N>` branches, hand off directly (`writer → reviewer → lead`), stop specialists when done, and rotate `lead`'s session across waves.
5. **Three measured live E2E hillclimb waves on `duboc/bananacast`:** Execute 3 iterative waves (`hc1`, `hc2`, `hc3`) on the new GCP project, measuring setup time/commands, `agent.log` error counts, active tokens per issue, 30-minute idle soak token/pod burn, and wall-clock delivery time — codifying improvements between waves and documenting all findings in `docs/gke-hillclimb-report.md`, `docs/lessons-learned.md` (`L34+`), `docs/gke-setup-runbook.md`, and `README.md`.

### Non-Goals
- Multi-tenant admission policies, custom Hub roles (`factory-owner`), or BigQuery fleet warehouses — those already live on `feat/multi-project-factory` and will rebase onto these core `scion-defaults` improvements afterward.
- Supporting Claude Code, OpenCode, or Hermes in the hillclimb runs (all roles run on **Antigravity + Gemini 4 Argon**; existing non-Antigravity code paths must stay unbroken in unit tests).
- Deploying `bananacast` application containers to public Cloud Run during the hillclimb (`bananacast` runs `infrastructure.deploy: none` with full L1–L5 local/in-process test & security verification, matching the `L30/L31` baseline).

### Anti-Goals (Failure Conditions Even If It Technically Works)
- **Any LLM token spend during an idle factory window** after the idle grace period expires (e.g., keeping a slower cron like `0 * * * *` that still wakes `lead` when nothing changed).
- **Mutating `duboc/bananacast` `main` or `factory/main`**, or leaving open test issues behind after hillclimb runs complete.
- **Masking or filtering `agent.log` errors** instead of fixing the underlying `transport-token` and `SCION_TELEMETRY_CLOUD_PROVIDER` root causes.
- **Committing any real GCP project ID, project number, billing account ID, org ID, email, cluster/VM name, or token** into tracked files (`AGENTS.md` Rule 1).
- **Mutating an external Scion checkout** outside `build/`.

---

## 3. Constraints

1. **Repository & Security Rules (`AGENTS.md`):**
   - Zero secrets, PATs (`ghp_*`, `github_pat_*`, `scion_pat_*`), emails, or real GCP identifiers in tracked files. All live hillclimb configuration lives in git-ignored `config/*.env` and `build/`.
   - Every provisioning/setup script supports `DRY_RUN=1` or `--dry-run`, uses `set -euo pipefail`, and sets `CLOUDSDK_CORE_DISABLE_PROMPTS=1`.
   - Control-plane runtime scripts remain **Python 3.12 stdlib-only** (mounted into `python:3.12-slim` K8s `CronJob` pods via `ConfigMap`).
   - Firestore holds control-plane bookmarks/state only (`factory_store.py`); no Spanner or provisioned databases added to the factory.
2. **Backwards Compatibility:** Existing unit tests (`372 passed`) and legacy opt-in modes (`wake_mode: "schedule"`, explicit `project.eval_slug`) continue to pass without regression.
3. **README Style Contract:** Any updates to `README.md` must satisfy `tests/test_docs_contract.py` (Google Developer Style + ASD-STE100: second-person `"you"`, zero contractions, `<= 25` words per sentence, all links/anchors valid).

---

## 4. Decisions Locked With the User vs. Author's Calls

### Decided With the User
- **Approach:** Run multiple live end-to-end GKE runs, hillclimb empirically across iterations, fix bottlenecks, and document findings in `scion-defaults`.
- **Branch Strategy:** Improve core `scion-defaults` on a dedicated branch off `main` (`feat/gke-factory-hillclimb`) before layering `feat/multi-project-factory`.
- **Testbed Infrastructure & Ephemeral Lifecycle:** Every E2E hillclimb run creates a **brand-new GCP project** from zero (`scion-gke-hc<k>-<6hex>` in `us-east1`, `SQL_AVAILABILITY_TYPE=ZONAL`, `VERTEX_LOCATION=global`), executes the full end-to-end run, exports transcripts/logs/metrics locally (`0700`), and **deletes everything at the end of the run** (`gcloud projects delete` + closing/cleaning up GitHub test artifacts) so cold-start GKE setup is hillclimbed on every iteration and `$0` cloud infrastructure ever lingers.
- **Target Workload:** `https://github.com/duboc/bananacast` on isolated hillclimb branches (`main` and `factory/main` untouched).
- **Harness & Model Pin:** **Only Antigravity (`antigravity-factory`) with `Gemini 4 Argon`** (`locations/global` on Vertex AI) for both `lead` and all specialists.
- **Orchestration Shape:** Single Hub project with on-demand `evaluator` (no split-brain `<slug>-eval` / `*/5` `eval-sweep`), zero-LLM Python delta gate + pod auto-sleep instead of `*/15` LLM heartbeats, ephemeral per-issue specialists on `factory/issue-<N>` with direct `writer → reviewer` handoff, and per-wave `lead` session rotation.

### Author's Calls (Explicit Defaults Chosen Here)
1. **`[Author's call]` Self-contained Scion source cache (`build/gke-hub/scion-src`):** When `--scion-repo` is omitted, `scripts/setup-gke-hub.sh` shallow-clones `https://github.com/GoogleCloudPlatform/scion.git` (pinned via `SCION_REF`, defaulting to `main`) into `build/gke-hub/scion-src` and applies the 4 GKE/Cloud Run Go patches strictly inside `build/gke-hub/scion-src` (if `--scion-repo` *is* passed, it copies the source to `build/gke-hub/scion-src` before patching so the caller's repo is never dirtied).
2. **`[Author's call]` Cold-start GCP project bootstrap (`--create-project`):** `scripts/setup-gke-hub.sh --create-project` creates `HUB_PROJECT` under `--folder-id` or `--org-id` (auto-discovering the active `gcloud` project's parent folder, or the single org folder / org root when unset) and links `--billing-account` (or the single open `gcloud billing accounts list` account), defaults `HUB_REGION=us-east1` (`VERTEX_LOCATION=global`) and `SQL_AVAILABILITY_TYPE=ZONAL` (halving Cloud SQL cost), creates a `default` VPC network if the org skips auto-creating it, enables required APIs (including `firestore.googleapis.com`, `logging.googleapis.com`, `cloudtrace.googleapis.com`, `monitoring.googleapis.com`), creates `HUB_IMPERSONATE_SA` (`factory-operator@<HUB_PROJECT>`) when org policies require SA impersonation for IAP/OIDC, and writes all discovered outputs directly to `config/factory.env` (`0600`) via `--write-env`.
3. **`[Author's call]` Single CLI lifecycle commands (`./scripts/scion-factory gke-up` & `gke-down`):**
   - `gke-up` chains `setup-gke-hub.sh --create-project --write-env`, `compile`, and `provision` (recording phase-by-phase elapsed timers in JSON).
   - `gke-down` snapshots pod `agent.log`s, `transcript_full.jsonl`s, K8s `CronJob` logs, and token/latency metrics into `~/scion-backups/<project>-<ts>/` (`0700`), closes/cleans up any run-specific GitHub test issues/branches, unlinks/deletes the ephemeral GCP project (`gcloud projects delete <HUB_PROJECT> --quiet`), and removes local ephemeral env/state files.
4. **`[Author's call]` Pre-start hook + refresher for PID 1 Direct IAP:** `make-factory-harness-configs.py` installs `.scion/hooks/pre-start.d/10-direct-iap-token` (`0755`) in `<harness>-factory` so `/home/scion/.scion/transport-token` AND `~/.scion/iap-access-token` receive a valid Workload Identity OAuth2 access token (`cloud-platform,userinfo.email,openid`) **before** `sciontool init` starts `StartHeartbeat`/`StartTokenRefresh` (line 756 vs line 1200 of `init.go`), and `factory-cd.sh` refreshes **both** files atomically every 25 minutes.
5. **`[Author's call]` Delta controller cadence, GitHub-only external wake triggers, & auto-sleep grace (`2 min` poll, `10 min` idle sleep, `25 turn` session rotation):**
   - `scion-factory-delta` runs inside the K8s control plane (`*/2 * * * *`) using `GITHUB_TOKEN` mounted from K8s Secret `scion-factory-github-auth` (`0400`/env).
   - **Wake triggers (no Hub message polling, no 5th Go patch):** Because Scion Hub rejects DMs to `stopped` agents (`409`) and delivers DMs to `running` agents directly into `tmux` (so re-sending them from Python would double-wake `lead`), `factory_delta.py` triggers solely on **GitHub issues/comments + Hub `list_agents` child pod states** (`error`, `halted`, or silent >30m). While `lead` is `running`, specialists message `lead` natively via `scion message`; while `lead` is `stopped` (which only occurs when `running_children == []`), GitHub is the sole external wake trigger.
   - **Shared-PAT comment attribution (`L14`):** Because factory agents and the operator share one GitHub PAT `user.login`, agent comments carry an explicit marker (`<!-- scion-agent: <slug> -->`), and `lead` posts its escalation comment *before* adding `status:po-decision`. `factory_delta.py` treats any newer comment without `<!-- scion-agent:` (or the `status:approved` label) as a human PO reply.
   - Idle grace before stopping `lead`: `IDLE_STOP_GRACE_SEC=600` (10 minutes with 0 actionable open issues and 0 running child agents).
   - Session rotation threshold: `LEAD_MAX_TURNS_BEFORE_ROTATE=25` turns (persisting state in `/scion-volumes/scratchpad/STATE.json` + GitHub issue labels/comments, and rotating only when no specialist handoff is mid-flight).
6. **`[Author's call]` Repeatable 3-issue hillclimb waves & unattended Wave 3 PO-reply drill on `duboc/bananacast`:** Because `duboc/bananacast@main` predates `factory/main`'s 31 commits while `duboc/bananacast` currently has 0 open unblocked product issues, each hillclimb wave `k ∈ {1, 2, 3}` creates an isolated trunk branch `factory/gke-hc<k>` from `origin/main` and seeds 3 representative multi-app issues labeled `factory:bananacast-hc<k>` (covering Python/Flask backend + pytest, FastAPI reverse proxy / React frontend + vitest/pytest, and security/config validation). In Wave 3's `status:po-decision` drill, the wave runner deterministically asserts the `0`-pod / `0`-token sleep window first and then posts the unmarked PO reply comment automatically so the run finishes unattended. At the end of each wave (after a 30-minute idle soak), the test issues are closed with `hillclimb-archive` and `main` / `factory/main` remain byte-for-byte untouched.

---

## 5. Architecture & Component Design

### 5.1 One-Command Cold-Start GKE Bootstrap

```text
./scripts/scion-factory gke-up bananacast.factory.yaml --env config/factory.env --create-project --apply --go
   │
   ├─► 1. scripts/setup-gke-hub.sh --env config/factory.env --create-project --harnesses antigravity --write-env config/factory.env --approve-apply
   │      ├─ Create GCP project (if missing) + link billing + ensure `default` VPC + enable APIs + Firestore DB
   │      ├─ Stage Scion source in build/gke-hub/scion-src (clone if --scion-repo omitted; copy if provided; never mutate caller repo)
   │      ├─ Apply 4 HA/GKE Go fixes inside build/gke-hub/scion-src
   │      ├─ Build & push only: hub, core-base, scion-base, scion-antigravity (skipping digests already in AR)
   │      ├─ Terraform apply shared-infra + two-pass Cloud Run Hub Direct IAP
   │      ├─ Set Hub-scope env: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION=global, GOOGLE_CLOUD_REGION=global,
   │      │                     SCION_TELEMETRY_ENABLED=true, SCION_TELEMETRY_CLOUD_ENABLED=true,
   │      │                     SCION_TELEMETRY_CLOUD_PROVIDER=gcp, SCION_GCP_PROJECT_ID=<HUB_PROJECT>
   │      └─ Atomically update config/factory.env (0600) with HUB_URL, HUB_IAP_AUDIENCE, cluster, namespace, registry
   │
   ├─► 2. scion_factory.compile_factory_boilerplate(spec, out=build/kit-<slug>)
   │      └─ Single-project Antigravity + Gemini 4 Argon kit (lead + specialists + on-demand evaluator, wake_mode=delta)
   │
   └─► 3. provision_factory.provision_factory(spec, kit_dir, config/factory.env, apply=True, send_go=True)
          ├─ setup-sandbox.sh (Workload Identity KSAs + logging/trace/monitoring/vertex roles)
          ├─ Single Hub project `<slug>` (no `<slug>-eval` project when eval_slug is empty)
          ├─ antigravity-factory harness config (with 10-direct-iap-token pre-start hook + factory-cd.sh) + templates
          ├─ K8s control plane (`scion-factory-sync` */1, `scion-factory-delta` */2, `scion-factory-watchdog` */5)
          └─ Zero LLM cron schedules; starts `<slug>-lead` (or lets delta controller wake `<slug>-lead` immediately on Wave 1 issues)
```

### 5.2 Pod Runtime Fixes (Zero `agent.log` Errors)

1. **Direct IAP Token Delivery Before PID 1 Loops (`config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token` + `factory-cd.sh`):**
   - Packaged by `scripts/make-factory-harness-configs.py` with mode `0755` (both local filesystem mode and `--via-api` Hub upload).
   - Runs during `lifecycleManager.RunPreStart()` inside `sciontool init` *before* `StartHeartbeat` and `StartTokenRefresh` start.
   - When `SCION_TRANSPORT_MODE=iap` and `SCION_TRANSPORT_AUDIENCE` starts with `369001918367-`, fetches a Workload Identity access token with `?scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid` (retrying cold GKE metadata server up to 15s) and writes it atomically (`0600`) to **both** `$HOME/.scion/transport-token` and `$HOME/.scion/iap-access-token`.
   - `factory-cd.sh` calls the same shared helper every `FACTORY_CD_IAP_REFRESH_SEC` (`1500`s) to refresh **both** files atomically.
2. **Cloud Telemetry Environment Completeness:**
   - `setup-gke-hub.sh` and `provision_factory.py` configure `SCION_TELEMETRY_CLOUD_PROVIDER=gcp` alongside `SCION_GCP_PROJECT_ID`, `SCION_TELEMETRY_ENABLED=true`, and `SCION_TELEMETRY_CLOUD_ENABLED=true`.
   - `scripts/setup-sandbox.sh` grants `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/monitoring.metricWriter` to the worker GSA on `SCION_GCP_PROJECT_ID` (`SANDBOX_PROJECT` / `HUB_PROJECT`).
3. **Autopilot Eviction Handling in `scripts/factory-watchdog.py`:**
   - In `run_api_watchdog` Check 3, treat `phase == "halted"` (and `exit_reason == "evicted"` / `container_status` starting with `"Failed"`) as recoverable when `lead` is not marked `lead_sleeping` in Firestore (or when an active child agent has an open in-flight assignment), calling `ensure_agent_started(pid, slug, force_resume=True)` subject to `MAX_RESTARTS_PER_HOUR`, and deleting stale `halted` ephemeral specialists whose issue has already reached `status:verified` or closed.

### 5.3 Single-Project Team & Delta-Driven Orchestration

#### Single Hub Project (`eval_slug` Optional)
- In `config/factory.schema.json`, `scripts/scion_factory.py`, and `scripts/provision_factory.py`:
  - `project.eval_slug` becomes optional (omitted by default in new GKE/single-project specs).
  - When `eval_slug` is omitted:
    - `provision_factory.py` creates **only** the primary Hub project `<slug>`, pushes build + `evaluator` role templates into `<slug>`, and starts **no** `eval-lead` pod or `eval-sweep` schedule.
    - `evaluator` (`role_library.py`) is an on-demand specialist inside `<slug>`, invoked by `lead` once per issue or wave to execute the L1–L5 verification suite cleanly against `<build_branch>` and post structured pass/fail evidence.

#### Zero-LLM Python Delta Controller (`scripts/factory_delta.py`)
- Stdlib-only Python module executed every 2 minutes by K8s `CronJob` `scion-factory-delta` (packaged in `scion-factory-control-scripts` `ConfigMap` by `install-k8s-control-plane.py`, with `GITHUB_TOKEN` in optional K8s Secret `scion-factory-github-auth` and `GITHUB_REPO` / `ISSUE_LABEL` passed via env).
- Persists per-project cursor in Firestore `factory_control_state/delta__<slug>`:
  - `last_fingerprint`: SHA-256 of canonical JSON `{actionable_issues, po_replies, failed_or_stalled_children}`.
  - `idle_since`: epoch timestamp when `actionable_issues == []`, `po_replies == []`, and `running_children == []`.
  - `lead_sleeping`: bool.
  - `lead_turns_in_epoch`: int.
- **Why Hub messages are NOT polled by `factory_delta.py`:** Scion Hub rejects `POST .../agents/{lead}/message` with `409 Conflict` when `lead` is `stopped`, and injects messages directly into `lead`'s `tmux` when `lead` is `running` (so echoing them from Python would double-wake `lead`). Therefore, while `lead` is `running`, specialists wake `lead` natively via `scion message`; while `lead` is `stopped` (which only occurs when `running_children == []`), GitHub issues/comments are the sole external wake trigger.
- **Classification of GitHub Issues (`GET /repos/{owner}/{repo}/issues?state=open&labels={label}`):**
  - **Ignored:** issues carrying `digest` or `hillclimb-archive`.
  - **Waiting on PO (non-actionable):** issues carrying `status:po-decision` or `status:blocked` whose latest comment still carries an agent marker (`<!-- scion-agent: ... -->`); becomes **actionable (`po_replies`)** immediately when a newer comment lacking `<!-- scion-agent:` arrives or `status:approved` is added.
  - **Actionable:** open issues not in `status:verified`, `status:po-decision`, or `status:blocked`.
- **Actions per 2-minute tick:**
  1. **Actionable delta (`fingerprint != last_fingerprint` and work/replies/failures exist):**
     - If `lead` is not running (`stopped`, `halted`, missing), start/resume `lead` (`ensure_agent_started`) and wait for `tmux`.
     - If `lead_turns_in_epoch >= LEAD_MAX_TURNS_BEFORE_ROTATE` (default `25`) and `running_children == []`, delete and freshly start `lead` so it boots with a clean context reading `/scion-volumes/scratchpad/STATE.json` + `INDEX.md`.
     - Send **one** compact structured message to `lead` summarizing the delta (issue numbers/titles/labels, PO replies, and child pod states) so `lead` acts immediately without polling commands, then update `last_fingerprint` and increment `lead_turns_in_epoch`.
  2. **Work in flight, no new delta (`running_children != []`):**
     - Do nothing (`0` messages sent, `0` `lead` tokens spent) unless a child has exceeded `CHILD_SILENCE_TIMEOUT_SEC` (`1800`s) or entered `error`/`halted`.
  3. **Completely idle (`actionable_issues == []`, `po_replies == []`, `running_children == []`):**
     - Record `idle_since` if unset. Once `now - idle_since >= IDLE_STOP_GRACE_SEC` (`600`s):
       - Stop/delete any leftover stopped/idle child pods in `<slug>`.
       - Stop `lead` (`POST /api/v1/projects/{pid}/agents/{lead}/stop`) if still running, set `lead_sleeping = True`, `lead_turns_in_epoch = 0`.
       - Subsequent ticks while `fingerprint == last_fingerprint` make 2 lightweight HTTP GETs (GitHub + Hub) in `<2s` and spend **zero LLM tokens and zero GKE agent pod CPU/RAM**.

#### Ephemeral Per-Issue Specialist Flow (`scion_factory.py` + `role_library.py`)
When `wake_mode == "delta"` (default for single-project GKE kits):
1. **Plan & Persist (`lead` Turn 1):** `lead` updates `/scion-volumes/scratchpad/STATE.json`, posts a brief plan on `#N` (`status:in-progress`), writes `/scion-volumes/scratchpad/briefs/<N>-<role>.md`, and starts **only** the required writer (`scion start <prefix>-<role> -t <role> --harness-auth vertex-ai -b <build_branch> ...`) and `<prefix>-reviewer` if not already running. Unused roles (`researcher`, `investigator`, `doc-writer`, etc.) are **never** started unless `#N` requires them.
2. **Implement & Direct Peer Handoff (`writer` Turn 1):** Writer creates/switches to `factory/issue-<N>` from `origin/<build_branch>`, implements with TDD, runs repo test/lint gates, **commits and pushes `origin/factory/issue-<N>` directly** (no scratchpad `.patch` files, no waiting on `lead` for a commit slot), and messages `<prefix>-reviewer` directly with the branch name, commit SHA, and test output.
3. **Independent Review (`reviewer` Turn 1):** Reviewer fetches `origin/factory/issue-<N>`, audits `origin/<build_branch>...origin/factory/issue-<N>`, and runs tests independently:
   - **`FAIL` (first attempt):** Reviewer messages `<prefix>-<writer>` directly with concrete fixes needed (`lead` is **not** woken!).
   - **`PASS` (or second `FAIL`):** Reviewer writes `/scion-volumes/scratchpad/reports/<N>-review.md` and messages `lead` **once** with `PASS <sha>` (or `FAIL` escalation).
4. **Integrate, Evaluate & Tear Down (`lead` Turn 2):**
   - On `PASS`, `lead` fetches `origin/factory/issue-<N>`, fast-forwards/rebases onto `<build_branch>` and pushes `origin/<build_branch>`, deletes remote `factory/issue-<N>`, updates `STATE.json`, and either triggers `<prefix>-evaluator` (at wave completion or per issue) or marks `#N` `status:verified` with commit + review + test evidence.
   - Whenever no further task in the current wave needs a running specialist, `lead` stops/deletes that specialist (`scion delete <specialist> --yes` or `scion stop <specialist>`) immediately so idle pods never linger on GKE Autopilot.
5. **Ignoring `state-change` Noise:** `lead`'s prompt explicitly instructs: if woken by a `system` / `state-change` (`COMPLETED`) event that carries no new instruction or actionable delta, run zero shell commands and end the turn immediately.

---

## 6. Three-Run Ephemeral E2E Hillclimb Plan on `duboc/bananacast`

Every hillclimb run `k ∈ {1, 2, 3}` executes the **full ephemeral lifecycle** — creating a brand-new GCP project (`scion-gke-hc<k>-<6hex>`), running against `duboc/bananacast` using **Antigravity + Gemini 4 Argon** (`infrastructure.deploy: none`, `infrastructure.runtime: kubernetes`), archiving logs/metrics locally (`0700`), and **deleting the GCP project + GitHub test branches/issues at the end of each run**.

| Metric | Previous Baselines (`bananacast` VM / `k8s-version` GKE) | Hillclimb Target (Run 3) |
|---|---|---|
| **Cold-start GKE commands / manual edits** | 4+ scripts + manual Go edits + manual `factory.env` copy-paste | **1 command** (`scion-factory gke-up`), **0** manual edits |
| **`agent.log` IAP `401` / telemetry errors** | `39,000–43,000` errors per pod + telemetry boot failure | **`0` errors** across all pods |
| **Unrecovered pod evictions** | `1` permanent `halted` (`exit 255`) | **`0` unrecovered** (verified under forced eviction in Run 3) |
| **`lead` active turns per issue** | `~8.5` turns/issue (`k8s-version`) | **`<= 4` turns/issue** |
| **Idle soak token & pod burn** | `~350K` uncached + `~500K` cached (`k8s-version`) / `933M` `eval-lead` | **`0` LLM tokens, `0` running agent pods** |
| **Post-run lingering cloud cost** | Manual multi-hour backup + project delete | **`$0`** (`scion-factory gke-down --delete-project` after every run) |

### Full-Lifecycle Execution Loop (Repeated 3x)
- **Run 1 (`scion-gke-hc1-*` / `factory/gke-hc1`):** Cold-start brand-new GCP project + GKE HA + `scion-antigravity` via `scion-factory gke-up --create-project`, seed 3 real `bananacast` multi-app issues (`factory:bananacast-hc1`), run end-to-end + idle soak, archive transcripts/logs/metrics & delete everything via `scion-factory gke-down --delete-project`, score Run 1, and identify top remaining setup/token/latency bottlenecks.
- **Run 2 (`scion-gke-hc2-*` / `factory/gke-hc2`):** Implement Run 1 code/prompt/controller refinements with unit tests, execute a second full cold-start `gke-up --create-project` → 3-issue `bananacast` wave + idle soak → `gke-down --delete-project`, and verify monotonic improvement across cold-start speed, pod health, and token cost.
- **Run 3 (`scion-gke-hc3-*` / `factory/gke-hc3`):** Apply Run 2 refinements, execute a third full cold-start `gke-up --create-project` **plus two live resilience drills**:
  1. Mid-issue `kubectl delete pod` on an active specialist to simulate GKE Autopilot eviction and verify automatic recovery.
  2. One `status:po-decision` issue to verify `lead` goes completely to sleep (`0` pods, `0` tokens) while waiting on the PO, and wakes within 2 minutes once an unmarked PO reply comment lands.
  Then run `scion-factory gke-down --delete-project` so zero GCP resources or GitHub test branches/issues remain.
- **Documentation & Codification:** Write `docs/gke-hillclimb-report.md` (side-by-side Run 1 / Run 2 / Run 3 scorecard vs. previous baselines) + `L34+` in `docs/lessons-learned.md` + updated `docs/gke-setup-runbook.md` and `README.md`.

---

## 7. What Is Left to the Builder

The planner and implementers decide internal module decomposition and exact helper signatures within these boundaries:
- Exact CLI flag names on `scripts/factory_delta.py` and internal Firestore field layout under `factory_control_state/delta__<slug>`.
- Exact JSON schema of `/scion-volumes/scratchpad/STATE.json`.
- Helper factoring between `config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token` and `config/harness/home/.local/bin/factory-cd.sh` (either a shared script in `.local/bin/` invoked by both, or self-contained POSIX `sh` + `python3`).
- Exact choice of the 3 representative `bananacast` tasks per wave (provided each wave mixes Python/Flask backend + FastAPI proxy/React frontend + tests so multi-specialist routing is exercised fairly across Waves 1–3).
