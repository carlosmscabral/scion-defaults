---
name: scion-factory-bootstrap
description: Use when a user wants to bootstrap a new Scion autonomous software factory for any repository or domain, or asks to be grilled or interviewed to generate Scion agent templates, a knowledge plane, an evaluation pyramid, and backlog waves from the scion-defaults lessons.
---

# Scion factory bootstrap

You conduct a structured interview with the human product owner, record the answers in `factory.yaml`, and compile a
Scion software factory kit with the deterministic tool in this repository. The tool never interviews the human and
never waits for keyboard input. You own the conversation and the reading of prose; the tool owns detection,
validation, and compilation.

Run all commands from the root of the `scion-defaults` repository.

## Rules

- Ask about one topic per turn. Don't send the whole question bank at once.
- Never invent intent. Mission, non-goals, Git choice, IAM roles, requirements, invariants, decisions, metrics,
  thresholds, latency targets, and backlog items come from the human. You can propose candidates, including ones you
  extracted from the repository, but the human must confirm each one.
- Challenge vague answers. When an answer isn't testable, quote the question's `challenge_prompt` and cite its
  lessons from `docs/lessons-learned.md`.
- Don't ask about static facts that the tool already detected. Ask the human to confirm them instead.
- Don't compile until `check` reports `ready=True`.
- Steer toward serverless. The factory control plane is always Firestore; never propose Spanner or another
  provisioned database for it. For the product, default to Cloud Run, Firestore, BigQuery, Cloud Storage, and Vertex
  AI, and record a provisioned store (Spanner, AlloyDB, Cloud SQL) only with the requirement that justifies it.

## Pre-Bootstrap Option: Greenfield Ideation Studio
If the product requirements and visual direction are unsettled, run a lightweight Ideation and Domain Consultant Studio first. Shut down the studio containers before you start the Build and Eval factory.

## Step 0: Target Hub mode

Ask the user for the target Hub mode: **New Full GKE Scion Hub**, **Existing GKE / Cloud Run Scion Hub**, or **Existing Single-VM Hub**.

1. **Fast host & GCP preflight (before any Cloud Build or Terraform apply):**
   - Verify required local tools on `PATH`: `gcloud`, `gh`, `git`, `jq`, `openssl`, `python3`, `uv`, plus `terraform` (`>= 1.9`) and `kubectl` with `gke-gcloud-auth-plugin` for GKE modes.
   - On Google Cloud organizations that restrict end-user OIDC/IAP tokens from workstations, set `USE_OPERATOR_SA=1` (or `HUB_IMPERSONATE_SA`) so `setup-gke-hub.sh` and `hub_client.py` impersonate `factory-operator@<hub-project>.iam.gserviceaccount.com`.
   - Confirm that every harness model chosen in the roster is enabled in Vertex AI Model Garden for the target project (for example, `Gemini 4 Argon` with `GOOGLE_CLOUD_LOCATION=global`, or Anthropic Claude models before selecting the `claude` harness).
2. **GKE bootstrap path:**
   - **Turnkey new GKE Hub + factory (`Recommended` for fresh projects):** After Steps 1 to 4 compile `factory.yaml`, run `./scripts/scion-factory gke-up factory.yaml --env config/<slug>.env [--create-project --billing-account <id> --folder <id>]` (dry-run first, then `--apply`). `scripts/setup-gke-hub.sh` automatically clones the pinned `SCION_REF` when `--scion-repo` is omitted and builds `linux/amd64` images by default (`IMAGE_PLATFORMS=linux/amd64`).
   - **Standalone Hub-only install:** Run `DRY_RUN=1 scripts/setup-gke-hub.sh --tier <terraform-ha|hybrid-gke> --harnesses <csv> --dry-run`, review the plan, and apply with `--approve-apply`.

## Step 1: Detect static facts and write the draft

Run the following command. Replace `TARGET_REPO_PATH` with the target repository, or omit `--repo` if no repository
exists yet:

```bash
./scripts/scion-factory grill --repo TARGET_REPO_PATH --out factory.yaml --json
```

The JSON output contains:

- `draft_path`: The draft specification. Static facts are filled in; intent fields are empty.
- `detected`: The language, commands, port, cloud stores, `knowledge_sources` (requirements, ADRs, contracts, design
  docs, and guides found in the repository), and `vcs` (whether the repository uses Git, and its GitHub remote).
- `proposed_roles`: The candidate specialist roles proposed by repository discovery, each with `role`, `team`, and `evidence`.
- `readiness`: The current readiness report. It fails for a new draft.
- `questions`: The unresolved interview questions, each with `pillar`, `question`, `why_we_ask`, `lessons`,
  `challenge_prompt`, and `field_path`.

If `factory.yaml` already exists, the command reuses it. Add `--force` only when the human wants to start over.

Show the human a short table of the detected facts, and ask them to confirm or correct it.

## Step 2: Interview the human, one topic at a time

Work through the `questions` in pillar order. For each question:

1. Ask the question in plain language, and explain `why_we_ask` in one sentence.
1. If the answer is vague, quote the `challenge_prompt` and ask again.
1. Write the confirmed answer into `factory.yaml` at `field_path`.

| Pillar | Fields to fill |
|---|---|
| `1_mission_and_boundaries` | `project.name`, `project.mission`, `project.non_goals`, and the `vcs` section |
| `2_runtime_data_and_iam` | `stack.*` commands, `infrastructure.runtime` (`kubernetes` or `docker`), `infrastructure.runtime_profile` (`"remote"` for Cloud Run Terraform HA, `"spot"` for GKE Autopilot Spot workers, `"gke"` for Hybrid VM+GKE, or `""` for default), `infrastructure.worker_sa_name` (default `"agent-worker"`), `infrastructure.deploy` (`service` or `none`), `infrastructure.cloud_stores`, `infrastructure.deployer_iam_roles` |
| `3_roster_models_and_cost` | `team.build_roles` (with optional role-scoped `skills`), optional `team.eval_roles` (omit `project.eval_slug` and place `evaluator` in `build_roles` for single-project `heartbeat_mode: delta`), `team.dropped_defaults`, `team.autonomy`, `team.heartbeat_mode` (`delta` or `direct-lead`), `team.max_wip`, `infrastructure.budget_usd`. Prefer upstream skills, strip stack contradictions via `audit-skills`, and cap at 6 skills per role. |
| `4_evaluation_pyramid_oracle` | `evaluation_pyramid.l1_health` through `evaluation_pyramid.l5_latency_slo` |
| `5_backlog_waves_and_autonomy` | `waves[*].issues[*]` with `owner_role`, `acceptance_criteria`, and `requirements` (REQ- IDs) |
| `6_knowledge_plane` | `knowledge_plane.sources`, `knowledge_plane.requirements`, `knowledge_plane.invariants`, `knowledge_plane.decisions`, `knowledge_plane.glossary` |

### Team: roster, proposed specialists, and autonomy

The draft specification starts with the default roster:
- **Build team:** `lead`, `backend`, `reviewer`, plus the three default specialists: `researcher`, `doc-writer`, and `investigator`.
- **Evaluation team:** `eval-lead` and `domain-evaluator`.

Work through the team configuration with the human:

1. **Deploy & heartbeat mode:** Ask for `infrastructure.runtime` (`kubernetes` or `docker`), `infrastructure.runtime_profile` (`"remote"` on Full GKE Cloud Run HA, `"spot"` for GKE Autopilot Spot workers, `"gke"` on Hybrid VM+GKE, or `""`), `infrastructure.worker_sa_name` (default `"agent-worker"`), `infrastructure.deploy`: `service` (the factory deploys and tests a live service) or `none` (a library, CLI, or monorepo package with nothing to deploy), and `team.heartbeat_mode`: `delta` (`Recommended` on GKE with Git: single Hub project, zero-LLM 1-minute `scion-factory-delta`, automatic 0-pod idle sleep) or `direct-lead` (15-minute LLM schedule across build + eval projects). With `deploy: none`, do not add `deployer` or `eval-deployer` roles.
2. **Review proposed specialists:** Present the default roster and each item in `proposed_roles` with its evidence. For each specialist the human confirms, run:
   ```bash
   ./scripts/scion-factory add-role factory.yaml --role R --evidence "..."
   ```
3. **Drop defaults only with a reason:** If the human decides to drop any of the default specialists (`researcher`, `doc-writer`, `investigator`), record the drop and reason under `team.dropped_defaults`:
   ```yaml
   team:
     dropped_defaults:
       - role: researcher
         reason: "complete static specification and no external dependencies"
   ```
4. **Autonomy level:** Ask for `team.autonomy`: `standard` (default; agents decide implementation and tell the PO about verified work, while scope, cost, IAM, ADRs, and acceptance wait for the PO), `supervised` (the PO approves the plan on GitHub before implementation starts), or `autonomous` (only cost, IAM, and acceptance wait for the PO).
5. **Cross-family verification and reviewer capacity:** New drafts set `team.verification_policy: cross-family` so builders and verifiers use distinct model families. Run `./scripts/scion-factory diversify-team factory.yaml` if role edits reintroduce monoculture. If `team.max_wip >= 4`, set `team.reviewer_replicas: 2` to avoid review bottlenecks. Use `team.verification_policy: single-harness` with `team.single_harness_reason` only when the Hub exposes one harness.
6. **Optional domain sub-coordinators (3-tier hierarchy for complex repositories):** Default to the flat 2-tier team (`lead -> specialists`) for small or medium repositories (`< 8` non-lead build roles or Single-VM `runtime: docker`). When the repository spans **3+ disjoint subsystems** (for example `backend`, `data-api`, and `frontend`) and **8+ non-lead build roles** on Kubernetes, offer optional domain `sub-coordinator` roles so Scion's native ancestry tree routes child notifications per domain while `lead` retains sole trunk-merge, deployment, and PO-inbox authority:
   ```bash
   ./scripts/scion-factory add-role factory.yaml --role sub-coordinator --as data-api-sub-coordinator --paths services/data-api
   ./scripts/scion-factory add-role factory.yaml --role backend --as api-builder --reports-to data-api-sub-coordinator --paths services/data-api/api
   ./scripts/scion-factory add-role factory.yaml --role security-auditor --as iap-security --reports-to data-api-sub-coordinator --paths services/data-api/auth
   ```
   Never nest sub-coordinators deeper than 3 tiers (`lead -> sub-coordinator -> specialist`), assign disjoint repo-relative `focus.paths` per sub-coordinator (include `<monorepo.subdir>/...` in monorepos), and keep `deployer`, `evaluator`, and `kaizen-analyst` directly under `lead`.
7. **Never invent intent:** Present candidates and evidence to the human first. Confirm every specialist addition, sub-coordinator subtree, default drop, autonomy level, and deploy choice before updating `factory.yaml`.

### Git is optional

Ask whether the human wants Git and GitHub now. If the repository already has a GitHub remote, the draft enables Git
and pre-fills `vcs.repo`; confirm it.

- **With Git:** set `vcs.enabled: true`, `vcs.repo: OWNER/NAME`, and two different branches. The backlog is GitHub
  issues, and the evaluation team uses an orphan branch.
- **Without Git:** set `vcs.enabled: false`. The backlog is `backlog/issues.json` (the lead is the only writer),
  deploys use workspace snapshots, and the evaluation team gets its own Hub project workspace. Tell the human that
  they can add Git later; see [Add Git later](#add-git-later).

### Monorepo: one factory per product folder

If the repository holds several products and this factory covers one folder (for example `pkg/kubelet`), set
`monorepo.subdir` to that folder and `monorepo.agent_prefix` to `project.slug`. Ask for build, test and lint
commands scoped to the folder (`go test ./pkg/kubelet/...`), never for the whole repository. The compiled kit then
names agents `<agent_prefix>-<role>`, starts them in the folder, and keeps the knowledge plane in
`<subdir>/knowledge-plane`. Run the interview once per product; every factory gets its own slug.

### Build the knowledge plane

The knowledge plane is what agents read before they plan, implement, review, or deploy. Build it in one of two ways,
or combine them:

- **Learn it from the repository.** For every path in `detected.knowledge_sources`, confirm with the human that it's
  current, and keep it in `knowledge_plane.sources`. The kit links these files in place; it never moves them. Then
  read the files and propose requirements (with `REQ-NNN` IDs and acceptance criteria), invariants, and glossary
  terms that you extracted. Quote the source line for each proposal so the human can verify it. Don't copy existing
  ADRs into `knowledge_plane.decisions`; they're already linked through `sources.adrs`.
- **Generate it with the human.** When sources are missing or stale, elicit requirements, invariants, and
  architecture decisions in the interview. Record each decision with an `ADR-NNN` ID, a title, the context, the
  decision, and its consequences.

Every requirement needs at least one testable acceptance criterion. Every invariant must be something a reviewer can
verify in a diff or with a command. Every wave issue should cite the `REQ-` IDs it delivers.

## Step 3: Pass the readiness gate

```bash
./scripts/scion-factory check factory.yaml --json
```

For each entry in `missing_or_weak`, return to the matching pillar and resolve it with the human. Repeat until the
report shows `"ready": true`.

## Step 3.5: Mandatory Pre-Compile Adversarial and Container Readiness Gate
Before you compile the kit, run the independent zero-context adversarial review. Run the topology audit to verify container binary readiness, PATH configurations, and UID mapping requirements.
```bash
./scripts/scion-factory audit-topology factory.yaml
```
Verify the checklist before you proceed.

## Step 4: Compile and verify the kit

Ask the human for an output directory, `OUTPUT_DIR`, and then run:

```bash
./scripts/scion-factory compile factory.yaml --out OUTPUT_DIR
OUTPUT_DIR/scripts/verify-factory.sh
python3 OUTPUT_DIR/knowledge-plane/kp-check.py
```

`kp-check.py` reports broken links to learned sources when it runs outside the target repository. That's expected:
the links resolve after the human copies `knowledge-plane/` to the root of the build workspace. Tell the human to copy
with `cp -Rn` so that an existing knowledge plane in the repository is merged, never overwritten, and to run
`kp-check.py` again from the workspace.

Recompiling never overwrites `knowledge-plane/`, `backlog/`, or `eval-workspace/`, because agents edit them after
bootstrap. Add `--force-knowledge` only when the human asks to reseed them.


## Step 5: Automated provisioning

Once the kit is compiled, perform automated provisioning:

```bash
./scripts/scion-factory provision factory.yaml --kit OUTPUT_DIR --env config/<slug>.env
```

Review the dry-run output with the human first. After they approve:
- For `team.heartbeat_mode: delta` on an existing Hub, run `--apply` (omit `--go`; `scion-factory-delta` wakes `lead` from GitHub deltas). For a brand-new GKE Hub + factory, run `./scripts/scion-factory gke-up factory.yaml --env config/<slug>.env --apply` (and tear down ephemeral trials with `./scripts/scion-factory gke-down ... --delete-project --apply`).
- For `team.heartbeat_mode: direct-lead`, run `--apply --go`.

Summarize what was generated, and point the human to `docs/gke-setup-runbook.md` (GKE) or `docs/setup-runbook.md` (Single-VM).

## Add Git later

When a factory that started without Git needs Git:

1. Ask the human to create an empty GitHub repository and to confirm that the Hub OS user can push to it.
1. Run the generated script in dry-run mode on the Hub VM, and review its plan with the human:

   ```bash
   GITHUB_REPO=OWNER/NAME OUTPUT_DIR/scripts/enable-git.sh
   ```

   The script migrates `backlog/issues.json` to GitHub issues with their current status, repoints the knowledge plane
   index to GitHub issues, imports the build workspace into the build branch, and imports the evaluation workspace as
   an orphan branch with no shared history.

1. After the human approves, run it with `DRY_RUN=0`.
1. Switch the specification to Git mode, and recompile the kit:

   ```bash
   ./scripts/scion-factory enable-git factory.yaml --repo OWNER/NAME
   ./scripts/scion-factory compile factory.yaml --out OUTPUT_DIR
   ```

1. Push the regenerated templates to the Hub, and restart the agents so they pick up the Git protocol.
