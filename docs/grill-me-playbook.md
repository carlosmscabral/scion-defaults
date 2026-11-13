# Grill-me playbook

This page describes the interview that your coding agent conducts when it bootstraps a new Scion software factory. Use
it to prepare your answers, and to understand why the agent pushes back on vague answers.

The interview always runs through an agentic coding agent that follows
[`skills/scion-factory-bootstrap/SKILL.md`](../skills/scion-factory-bootstrap/SKILL.md). The
[`scripts/scion-factory`](../scripts/scion_factory.py) tool doesn't interview you. The agent calls the tool to
detect facts, write a draft, check readiness, and compile the kit.

## What the interview decides

A Scion software factory has two layers.

- **The factory kernel** is the same for every project, and this repository provides it:
  - Hub administration through the Hub API ([`scripts/scion-admin.py`](../scripts/scion-admin.py)).
  - A 5-minute watchdog with a circuit breaker ([`scripts/factory-watchdog.py`](../scripts/factory-watchdog.py)).
  - Stacked, multi-turn trace waterfalls ([`patches/sciontool-stacked-traces/`](../patches/sciontool-stacked-traces/)).
  - Branch isolation: `main` for the build team, and an orphan `eval-dashboard` branch for the evaluation team (L25).
  - Clean `git archive` deployments with `--to-latest` (L23).
  - Message delivery checks (L1) and explicit turn completion with `sciontool status` (L2).
- **The project contract** is different for every project, and the interview elicits it. The agent records the
  contract in `factory.yaml`, which follows [`config/factory.schema.json`](../config/factory.schema.json).

## How the agent runs the interview

1. The agent runs `scripts/scion-factory grill --repo TARGET_REPO_PATH --out factory.yaml --json`. The command
   detects static facts, such as the language, commands, port, cloud stores, knowledge sources (requirements, ADRs,
   contracts, design docs, and guides), and the GitHub remote, and writes a draft. Intent fields stay empty.
1. The agent shows you the detected facts and asks you to confirm them.
1. The agent asks the unresolved questions one topic at a time, in pillar order. When an answer isn't testable, the
   agent quotes the question's challenge prompt and asks again.
1. For deploy and team, the agent asks whether `infrastructure.deploy` is `service` or `none`. It presents the default
   roster (including researcher, doc-writer, investigator), reviews candidate specialists from discovery with their
   evidence, adds confirmed specialists with `scion-factory add-role`, records drops with a reason under
   `team.dropped_defaults`, and asks for the autonomy level (`team.autonomy`).
1. For the knowledge plane, the agent reads the detected sources and proposes requirements, invariants, and glossary
   terms with a quote from each source. You confirm, correct, or reject each proposal. Where sources are missing, the
   agent elicits the same items from you.
1. The agent runs `scripts/scion-factory check factory.yaml` and resolves every gap with you.
1. When the check passes, the agent runs `scripts/scion-factory compile factory.yaml --out OUTPUT_DIR` and then
   `OUTPUT_DIR/scripts/verify-factory.sh`.

## Readiness gate rules

The `check` command fails, and the agent doesn't compile, if any of the following conditions is true:

- The project name, slug, or mission is missing or vague.
- Git is enabled, but `vcs.repo` isn't `OWNER/NAME`, a branch is missing, or both branches are the same (L25).
  When Git is disabled, none of these fields is required.
- `project.non_goals` is empty (L4).
- `infrastructure.deploy` is missing or not `service` or `none` (L29).
- `infrastructure.deploy` is `service` and `infrastructure.deployer_iam_roles` is empty (L5). With `deploy: none`,
  deployer IAM roles are omitted.
- `team.autonomy` is missing or not `supervised`, `standard`, or `autonomous`.
- A default specialist (`researcher`, `doc-writer`, `investigator`) is missing without a recorded reason in
  `team.dropped_defaults`.
- The build or test command is missing (L18).
- The build team has fewer than three roles, or the evaluation team has fewer than two roles (L25).
- Any role has no concrete responsibility.
- The L1 health endpoint, the L2 invariants, or the L4 invariants are missing (for `deploy: none`, L1 to L5 grade code
  and contracts directly).
- `evaluation_pyramid.l3_domain_quality.target_threshold` isn't a number (L15).
- `evaluation_pyramid.l5_latency_slo.p95_ms` isn't a number (L15).
- Wave 1 has no issues, or an issue has no acceptance criteria (L18).
- `knowledge_plane.requirements` is empty, or a requirement lacks a `REQ-NNN` ID, a title, or acceptance criteria.
- `knowledge_plane.invariants` is empty.
- A decision lacks an `ADR-NNN` ID, a title, or a decision.
- A wave issue cites a `REQ-` ID that isn't defined.

## Why each question exists

Each question maps to a failure that happened in production. The lessons are in
[`lessons-learned.md`](lessons-learned.md).

| Question ID | Pillar | What goes wrong if you skip it | Lessons |
|---|---|---|---|
| `mission_and_non_goals` | `1_mission_and_boundaries` | Agents build out-of-scope features, or send product decisions to your inbox. | [L4, L15, L25](lessons-learned.md) |
| `branch_isolation_topology` | `1_mission_and_boundaries` | Evaluation agents overwrite application code, or grade unmerged local files. Without Git, the evaluation team needs its own workspace for the same reason. | [L18, L23, L25](lessons-learned.md) |
| `stack_verification_commands` | `2_runtime_data_and_iam` | A broken commit in the shared `/workspace` blocks every specialist in the project. | [L18, L23](lessons-learned.md) |
| `deploy_target` | `2_runtime_data_and_iam` | Factories with nothing to deploy used placeholder services, stalling deployers and live checks. With `deploy: none`, deployers are omitted and evaluation grades code and API contracts directly. | [L29](lessons-learned.md) |
| `cloud_stores_and_deployer_iam` | `2_runtime_data_and_iam` | The deployer stops mid-wave with `PERMISSION_DENIED` on a data store or Cloud Run, or the product picks a provisioned database that a serverless store would serve. | [L5, L13, L23](lessons-learned.md) |
| `team_roster_and_harnesses` | `3_roster_models_and_cost` | Without default specialists, unfamiliar code gets built without research, bugs lack root causes, and docs drift. Without an autonomy level, agents either flood the PO or overstep decision rights. On large multi-subsystem repos (8+ specialists across 3+ domains), a flat root lead suffers notification storms unless optional `sub-coordinator` subtrees group domain builders (`reports_to`), while over-nesting small teams wastes hops and tokens. Each harness also has runtime requirements (L6, L7, L8, L16, L22). | [L4, L6, L7, L8, L9, L16, L22, L29](lessons-learned.md) |
| `heartbeat_and_budget_guardrails` | `3_roster_models_and_cost` | Waking an Opus lead every 15 minutes on an idle repository wastes tokens, and an uncapped WIP limit causes rebase conflicts. | [L9, L10, L17, L21, L24](lessons-learned.md) |
| `eval_l1_l2_contracts` | `4_evaluation_pyramid_oracle` | Expensive L3 and L4 evaluations run against broken or unmigrated endpoints. | [L15, L23](lessons-learned.md) |
| `eval_l3_metric` | `4_evaluation_pyramid_oracle` | Without a numeric domain metric, such as `ndcg_at_10` in the search factory, the evaluation team can't verify waves or file regression issues. | [L15, L25](lessons-learned.md) |
| `eval_l5_slo` | `4_evaluation_pyramid_oracle` | Agents add sequential LLM calls that pass unit tests but exceed production latency budgets. | [L15, L27](lessons-learned.md) |
| `wave_1_backlog` | `5_backlog_waves_and_autonomy` | The first heartbeat finds no ready backlog items with testable acceptance criteria to dispatch. | [L1, L2, L18](lessons-learned.md) |
| `knowledge_sources` | `6_knowledge_plane` | Agents can't open the real specs and guess file names; a wrong FAIL deadlocked the factory for about 60 hours. | [L4, L15](lessons-learned.md) |
| `knowledge_requirements_invariants` | `6_knowledge_plane` | Without requirement IDs and invariants, reviews become opinions and regressions ship untraced. | [L15, L18, L25](lessons-learned.md) |

## Worked examples

The agent can show you these presets when you need an example answer:

- [`starter-service.factory.yaml`](../presets/starter-service.factory.yaml): A serverless Go double-entry ledger on Cloud Run,
  Firestore, and BigQuery.
  The L3 metric is `reconciliation_accuracy >= 0.999`.
- [`psearch.factory.yaml`](../presets/psearch.factory.yaml): A 10-agent hybrid product search factory on Go,
  Spanner, BigQuery, and Vertex AI (the historical product stack; its control plane uses Firestore like every kit). The L3 metric is `ndcg_at_10 >= 0.80`. Its knowledge plane is learned from
  existing sources (`PRODUCT.md`, ADRs, and an OpenAPI contract).
- [`acme-airlines.factory.yaml`](../presets/acme-airlines.factory.yaml): A 14-agent airline disruption recovery
  factory on Python, FastAPI, React, and Spanner, with dedicated heartbeat sensor agents. The L3 metric is
  `constraint_satisfaction_rate >= 0.95`. Its knowledge plane is generated in the interview.
