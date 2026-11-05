# Multi-Project Scion Factory — Design Specification

**Date:** 2026-11-04 (revision 3: 2026-11-05)
**Repository:** `duboc/scion-defaults`
**Status:** Draft for review
**Builds on:** [GKE auto-factory design](2026-10-27-gke-auto-factory-design.md) (one factory on a GKE HA Hub, verified end to end at `cd842bb`)
**Picture:** [executive diagram](../diagrams/multi-project-scion-factory.html) ([text version](../diagrams/multi-project-scion-factory.md))

Revision 2 removed the Eval factory, moved every eval into BigQuery, switched the issue flow to one pull request per
issue, and recorded the platform admin's decisions (§10.1) and the answers to the builder review. Revision 3 runs the
scheduled evals inside BigQuery (scheduled queries that call stored procedures), lets factory agents start the same
evals on demand, and adds ASCII flow diagrams (§6.1).

---

## 1. Intent

### 1.1 What the platform admin asked for

In their words (typos fixed):

> The main goal is to use the same Scion deployment and not create one per project, so we would have one Scion HA GKE
> deployment with N projects, and that is the factory I want to build.

> I will delegate users responsible. They will check the agents that were created for them, we will define how the
> hooks of that work work, and they change what is needed, adapting on the fly for now. Later, with the findings, I want
> a self-learning loop that understands that and builds the new factory with the new default.

> The trajectories will need to be sent to logs and extracted to BigQuery, so the evals for agents get a bit different.

> I want to be able to check the cost by PR or issue, and eventually check which harness and model fared better on cost
> versus task completion, plus any other eval that is good to have on a factory of agents building software.

> Let's remove the eval factory and start dropping everything to BigQuery from the harness trajectories and logs, so we
> can run the eval from BigQuery without having a fleet of agents just to do eval. Let's incorporate some of those agents
> inside the builder factory, and increase and ensure telemetry so we can do everything from BigQuery.

> But the eval runs are driven by an agent from the factory and could be triggered. [...] Can we do this [the daily
> regression and the scheduled rubric scoring] with BigQuery?

### 1.2 Why

- **Cost and operations.** One HA stack (Cloud Run Hub behind IAP, Cloud SQL, Filestore, GKE Autopilot) has a fixed
  monthly floor and its own upgrade and patch work. One stack per project multiplies both.
- **Agents watching agents is waste.** Today every factory carries a second Scion project whose standing team exists
  only to measure the first. Most of that measurement is a query over data the build factory already produces; the rest
  belongs inside the build flow, per issue.
- **Teams adapt faster than the platform.** Owners see first what their agents get wrong. Letting them change templates
  and hooks in place removes the platform as a bottleneck, and what they change is the best evidence of what the
  defaults miss.
- **Leadership needs unit economics.** Cost per PR and per issue, and evidence for which harness and model to use, turn
  the factory from a demo into a budget line.

### 1.3 People

| Who | Does what |
|---|---|
| Platform admin | Runs the shared deployment, onboards and retires projects, owns the eval rubrics, reviews default upgrades. |
| Project owners (1 or more per project) | Steer their project's agents, review and merge the factory's PRs (merging is acceptance), change templates and hooks on the fly. They are the PO of their factory. |
| Manager and VP | Read cost and eval results. |

## 2. Terms

| Term | Meaning |
|---|---|
| Platform | The one shared deployment: Scion Hub (Cloud Run behind IAP), Cloud SQL, Filestore, the GKE Autopilot agent cluster, and the host GCP project they run in. |
| Factory | One onboarded project: one FactorySpec, **one** Build Scion project, one GitHub repo (or monorepo folder), one sandbox, one set of owners. "Project N" in the diagram is factory N. There is no Eval project. |
| Sandbox | The target GCP project a factory deploys and tests in. |
| Baseline | What a factory was onboarded with: the `scion-defaults` commit and the content hash of every owner-editable artifact. |
| Owner-editable / platform-owned | What an owner may change on the fly (§6.2.5), versus what only the platform changes. |
| Live edit (drift) | A change an owner makes in the Hub after onboarding to an owner-editable artifact. |
| Per-issue agent | An agent the lead creates for one issue (`issue-<N>-<role>`) and deletes when the issue is done. |
| Warehouse | The BigQuery dataset in the host project that holds every telemetry, outcome and eval table. |
| Eval run | One execution of an eval procedure in BigQuery, started by a schedule or by a factory agent. |

## 3. Where we start (`cd842bb`)

- **The platform runs** and was verified end to end (`docs/lessons-learned.md`, L33). `scripts/setup-gke-hub.sh`
  builds the Hub and harness images from an upstream Scion checkout with four source patches, and sets the Hub-scope env
  `GOOGLE_CLOUD_PROJECT` to `SANDBOX_PROJECT`, so model calls bill to the first factory's sandbox.
- **`scion-factory provision`** (`scripts/provision_factory.py`) stands up **one** factory from a FactorySpec and one
  `config/factory.env`: sandbox IAM, a Build and an Eval Scion project, GCP identity registration, shared dirs,
  `<harness>-factory` harness configs, role templates, the K8s control-plane CronJobs, the lead and eval-lead, and a
  heartbeat schedule (every 15 minutes).
- **Build flow.** The lead starts a standing team (for example backend, frontend, reviewer, deployer), works at most 2
  issues at a time, and the whole team commits to one shared trunk checkout with commit messages ending in `(#N)`. There
  are no pull requests. The owner accepts work by closing the issue.
- **Eval factory.** A second Scion project on an orphan branch, with eval-lead, factory-analyst (agent activity and
  issue flow from Firestore and GitHub), search-evaluator (golden queries, NDCG/MRR, latency and requirement probes
  against the live service, results in Firestore `eval_runs`), ui-craftsman (builds the eval dashboard) and
  eval-deployer (deploys that dashboard). `sync-factory-agents.py` copies agent state to Firestore
  `factory_agent_status` so the dashboard can read it without Hub access.
- **Telemetry.** Each harness calls `sciontool hook`; Cloud Trace shows stacked waterfalls; Cloud Monitoring counters
  `gen_ai.api.calls` and `scion.usage.tokens{token_type}` carry the Scion project, agent, harness and model. Nothing
  reaches BigQuery, and nothing links usage to an issue.
- **Upstream analytics log.** Upstream `sciontool` writes structured `agent.usage` events to the Cloud Logging log
  `scion-analytics` (Scion project and agent IDs, harness, model, calls, input, output, cache and reasoning tokens,
  optional harness-reported cost; primary key `(producer_namespace, event_id)`), and states that a Logging sink is meant
  to route it to BigQuery. It writes them only when the GCP exporter is active. `agent.lifecycle` is reserved but not
  produced (`pkg/telemetrycontract/`, upstream `9ce52bc`).

### 3.1 What breaks when a second factory joins the same Hub

| Today | Where | What goes wrong with factory #2 |
|---|---|---|
| One pair of control-plane CronJobs with `FACTORY_PROJECTS`, `SANDBOX_PROJECT` and `FIRESTORE_PROJECT` in their env | `install-k8s-control-plane.py` | Provisioning #2 re-applies the pair with #2's values; #1's status lands in #2's Firestore. |
| Control identity `factory-control@<SANDBOX_PROJECT>` on KSA `scion-factory-control` | `provision_factory.py` | #2 re-points the shared KSA to its own GSA; #1 loses access. |
| KSAs named `scion-worker` and `scion-deployer` for every factory | `setup-sandbox.sh` | Scion picks a pod's KSA by its GSA annotation; #2 re-annotates the same KSAs, so #1's agents can run as #2's identity. |
| Hub-scope `GOOGLE_CLOUD_PROJECT` = the first sandbox | `setup-gke-hub.sh` | Every factory's model spend lands in factory #1's sandbox, and every factory's GSA would need model access there. |
| The project admin role holds `project.update`, `project.manage` and harness-config writes; the `hub-member` role holds `project.create` | upstream Hub roles | An owner could register a foreign GSA (verification only checks that the Hub can impersonate it, and the Hub can impersonate every factory's GSAs), create a project of their own, or change the identity mode so a template's KSA applies as written. |
| Templates can set `kubernetes.serviceAccountName`, `kubernetes.namespace`, `volumes` and `image` | upstream `ScionConfig` | An owner edit can point a pod at another factory's KSA, the fleet control KSA, the namespace's `default` KSA, or another factory's files on the shared Filestore. |
| One `config/factory.env` for platform and factory values | all scripts | Onboarding #2 means editing the shared platform file. |
| The watchdog resumes any heartbeat schedule that is not `active` | `factory-watchdog.py` | An owner's pause is undone within 5 minutes. |
| Re-running provision uploads changed template files | `push-templates.py` | Owners' live edits are overwritten. |

## 4. Goals, non-goals, anti-goals

### Goals

1. **One platform, N factories.** Onboarding factory N+1 creates no new Hub, database, file store or cluster, creates
   exactly one Scion project, and changes nothing that running factories use.
2. **Delegated owners.** Each factory has at least one owner who sees and steers only that factory, merges its PRs, and
   changes its owner-editable artifacts on the fly.
3. **Live edits are never overwritten**, and edits that would break isolation cannot take effect. Every live edit is
   recorded as drift.
4. **Isolation.** A factory's agents can change only that factory's sandbox, files, credentials and repo folders (through
   owner-reviewed PRs), and can read only that factory's warehouse rows.
5. **Pay for work, not for waiting.** No standing eval team. Workers, reviewers and evaluators exist only while an issue
   is in flight; when a factory is idle, only its lead runs.
6. **Everything in BigQuery.** Full trajectories, usage, outcomes and logs land in the warehouse. Evals are computed
   there (SQL and Gemini rubric scoring), and telemetry completeness is tested and monitored, not assumed.
7. **Self-learning defaults.** A harvester correlates live edits with outcomes and opens reviewable PRs on
   `scion-defaults`, so factory N+1 starts from better defaults.

### Non-goals

- More than one Hub, sharding or federation (A2A bridge). Revisit at the scale trigger in §9.
- New Scion source patches beyond the four `setup-gke-hub.sh` already applies.
- An Eval Scion project for new factories, or a standing agent team whose only job is evaluation.
- Migrating the running factory in place. It is re-onboarded (§6.2.3).
- Upgrading running factories to new defaults automatically, or merging default changes without a human.
- Webhook-driven issue intake (upstream's GitHub App design defers it); the lead keeps polling on its heartbeat.
- Randomized harness/model experiments; comparisons are observational for now.
- Moving agents to the `substrate` (gVisor) runtime.
- A custom-built dashboard service. A Looker Studio report on the warehouse views replaces the eval dashboard.

### Anti-goals (failure even if it technically works)

- An owner or agent of factory A can see or change anything of factory B: agents, templates, files, secrets, repo
  folders, cloud resources or warehouse rows.
- Platform automation quietly reverts an owner's live edit.
- A secret reaches any log or table.
- Cost numbers that look exact but double count, or that do not reconcile with what Google bills.
- Idle cost that grows like (number of roles × N).
- Builders grading their own work: the agents that write a change can also rewrite the tests or rubrics that judge it.
- The harvester changes defaults without evidence and review, or copies one team's project-specific content into the
  shared defaults.

## 5. Constraints

- **Repo rules (`AGENTS.md`).** No secrets, real project IDs, emails or VM names in tracked files. Every script
  supports `DRY_RUN=1` (or `--dry-run`) and uses `set -euo pipefail` and `CLOUDSDK_CORE_DISABLE_PROMPTS=1`. Never
  `docker restart` agent containers. No Spanner. **This design changes rule 3:** Firestore keeps the control plane's
  own state (fleet registry, watchdog bookmarks), while agent status and evaluation data move to BigQuery through Cloud
  Logging. `AGENTS.md`, the README and `docs/architecture.md` change with Stage 1.
- **Scion facts.**
  - Agent tokens are scoped to one project, so onboarding runs outside the Hub with a platform admin identity.
  - Scion never creates KSAs. In `assign` mode it runs a pod as the KSA annotated with the agent's GSA; in other modes a
    template's `kubernetes.serviceAccountName` applies as written.
  - Agent pods carry the labels `scion.agent=true` and `scion.project_id=<project>`.
  - Harness configs can be Hub-global or project-scoped. Templates, project pre-start hooks, project env and schedules
    are per project. Only one project pre-start hook is active per project, and it replaces the Hub-level fallback.
  - The Hub scheduler runs in memory on one Hub instance. Scion pauses a schedule itself only with a reason in
    `statusReason`.
- **GitHub facts.** One identity cannot approve its own PR, so agent reviews are comments; the owner's approval and
  merge is the gate. Branch protection and CODEOWNERS need repo admin rights to configure.
- **GCP facts.** GSA account IDs are 6–30 characters. Cloud Monitoring metric labels cannot carry issue or PR numbers,
  so work context goes into log entries. A Cloud Logging entry is at most 256 KB. The Cloud Billing export to BigQuery
  does not backfill.
- **BigQuery facts.** `AI.GENERATE` calls Gemini from SQL and can return typed fields through `output_schema`. A remote
  function calls a Cloud Run endpoint from SQL, batches rows (`max_batching_rows`), and accepts and returns only scalar
  and JSON values. Scheduled queries run SQL scripts, including `CALL` of stored procedures.
- **Backward compatibility.** Existing single-factory installs keep working through `provision` until they are
  re-onboarded, and the existing test suite (366 tests at `cd842bb`) stays green.
- **Order.** Stage 1, then Stage 2, then Stage 3. Each stage ships, and is useful, on its own.

## 6. Design

### 6.1 Shape

1. **Stage 1 — Shared Hub for N factories:** onboarding, isolation, owners, live-edit protection, one PR per issue
   with per-issue agents (evaluators included), and a raw landing zone in BigQuery so that no event is lost while
   Stage 2 is built.
2. **Stage 2 — Everything in BigQuery:** full trajectories, telemetry completeness, cost, budgets, and the evals that
   replace the Eval factory.
3. **Stage 3 — Self-learning harvester:** evidence-backed PRs on `scion-defaults`.

#### 6.1.1 The platform at a glance

```text
OUTSIDE GOOGLE CLOUD          HOST PROJECT (one shared platform)                    PER FACTORY
--------------------          ----------------------------------                    -----------

                              +---------------------------------------------------+
Platform admin --onboard----->| IAP --> Scion Hub (Cloud Run)                     |
                              |           |    Cloud SQL, Filestore               |
Owners of A, B, ... --IAP---->|           v                                       |
  (review and merge PRs)      | GKE Autopilot, one runtime namespace              |
                              |   factory A: lead + issue-<N>-* pods (KSA a-*)    | --deploy--> Sandbox A
GitHub repos <--- PRs --------|   factory B: lead + issue-<N>-* pods (KSA b-*)    | --deploy--> Sandbox B
     |                        |   admission policy: a pod runs only as its        |
     |                        |   own factory's KSA, image and settings           |
     |                        |           |                       |               |
     |                        |       model calls              events             |
     |                        |           v                       v               |
     |                        | Vertex AI (Gemini, Claude)   Cloud Logging        |
     |                        |                                   |  sink         |
     | outcomes, evals/       |                                   v               |
     +--- hourly job -------->| BigQuery warehouse  <---  transcript bucket       |
                              |   views, procedures, Gemini rubric scores         |
                              |   --> Looker Studio report, budget alerts         |
                              |                                                   |
                              | Firestore: fleet registry, watchdog state         |
                              | Fleet CronJobs: sync, watchdog, drift             |
                              +---------------------------------------------------+
```

#### 6.1.2 One issue, end to end

```text
[ GitHub issue #42 ]
        |
        v
LEAD (heartbeat) ---- creates branch factory/issue-42 and starts the issue's agents
        |
        +--> issue-42-backend ........ codes, tests, pushes, opens PR "Closes #42"
        +--> issue-42-reviewer ....... PASS/FAIL review comment on the PR
        +--> deployer ................ preview: tagged Cloud Run revision, no traffic
        +--> issue-42-evaluator ...... CALL run_product_regression(<preview URL>) in BigQuery,
        |                              then posts the results on the PR
        +--> issue-42-ui-craftsman ... design review (UI issues only)
        |
        v
OWNER reviews the PR ---- requests changes? ----> lead re-briefs the issue's agents
        |
        |  merges (this is acceptance; #42 closes)
        v
deployer rolls the merged commit out to live traffic and runs the post-merge checks
        |
        v
lead stops and deletes issue-42-*            an idle factory runs only its lead

Every step writes events (usage, trajectory, hook verdicts, eval results) that land in BigQuery.
```

#### 6.1.3 Data in, evals out

Everything lands in the warehouse first:

```text
AGENT PODS AND FLEET CONTROL PLANE       TRANSCRIPTS            PLATFORM JOBS AND EXPORTS
----------------------------------       -----------            -------------------------
agent.usage (sciontool)                  full session JSONL,    GitHub: PRs, reviews, checks, reverts
factory.trajectory, hook verdicts        secrets removed        evals/ golden sets and probes
factory.agent, factory.drift             in the pod               (default branch only)
factory.eval_result, factory.escalation       |                 Cloud Billing export
          |                                   |                           |
          v                                   v                           |
Cloud Logging --> Log Router sink      transcript bucket                  |
          |                                   |  load                     | hourly job, export
          v                                   v                           v
  +---------------------------------------------------------------------------------------------+
  | BigQuery warehouse (host project): raw tables, partitioned by day                           |
  +---------------------------------------------------------------------------------------------+
```

Then the same two stored procedures run every eval, whoever starts them (§6.3.6):

```text
WHO STARTS AN EVAL RUN                BIGQUERY STORED PROCEDURES                CALLS OUT TO
----------------------                --------------------------                ------------
                                      +-----------------------------------+
daily scheduled query --------+       |                                   |
lead: each PR (preview) ------+       | run_product_regression(f, target) | --> remote function
lead: after each merge -------+------>|                                   |       --> preview or live service
owner: /eval or Hub message --+       | score_trajectories(f, scope)      | --> AI.GENERATE (Gemini)
analyst: weekly or on demand -+       |                                   |
                                      +-----------------------------------+
                                                        |
                                                        |
                                                        v
                  results and judgments (append-only: trigger, who asked, scope, rubric version)
                                                        |
                                                        |
                                                        v
                  views: cost per PR and issue, harness x model scorecard, 7-day survival,
                         thrash, hook catch rate, product quality
                                                        |
                          +-----------------------------+-----------------------------+
                          v                             v                             v
                  Looker Studio report            budget alerts               harvester (Stage 3)
```

#### 6.1.4 The learning loop

```text
scion-defaults @ commit C ---- onboard ----> factory k (baseline: C + artifact hashes)
        ^                                          |
        |                                          |  owners change prompts and hooks live
        |                                          v
        |                                   factory.drift events
        |                                          |
        |                                          v
        |                                   BigQuery: metrics and rubric scores
        |                                   before and after each edit
        |                                          |
        |  PR with an evidence table               v
        +----( a human reviews and merges )---- self-learning harvester (weekly)

The next factory onboards from the new commit C', and its baseline records C'.
```

### 6.2 Stage 1 — Shared Hub for N factories

#### 6.2.1 Platform settings and factory settings live apart

- Platform settings (Hub URL, IAP audience, host project, region, cluster, runtime namespace, image registry, control
  identity, registry location, warehouse dataset, transcript bucket) live in a platform env file: git-ignored, with a
  tracked example.
- Each factory's real values (sandbox project, owners, repo credential) come from its FactorySpec plus a git-ignored
  per-factory env file. Onboarding never edits the platform file.
- FactorySpec: `project.eval_slug`, `team.eval_roles` and `vcs.eval_branch` become optional and deprecated; new kits
  never contain them. The product evaluator, ui-craftsman and factory-analyst become optional build roles.

#### 6.2.2 Fleet registry

- A Firestore collection (`factory_fleet`) in a Firestore database in the host project, accessed through
  `factory_store.py`. One document per factory: Scion project slug and ID, sandbox project, repo and product folder,
  owners, identity names, baseline (`scion-defaults` commit and artifact hashes), current artifact hashes, schedules the
  platform paused, state (`active`, `paused`, `retired`), and timestamps.
- Only `onboard` and `retire` write it, except that the drift recorder writes its hash and drift fields. The control
  identity reads it. Owners never write it.
- `config/firestore/` (contract, rules, indexes) covers the new collection.

#### 6.2.3 Onboarding, retiring, and the clean start

The platform admin runs these from a workstation, Cloud Shell or CI. Both are dry runs by default, act with `--apply`,
and are idempotent.

`scion-factory onboard <factory.yaml> --kit <dir> --env <factory env> --owners <emails>`:

1. Validate the FactorySpec. Refuse when the slug is taken, or when the sandbox already belongs to another factory in
   the registry.
2. Create the factory's identities (§6.2.4) and sandbox IAM.
3. Create the Scion project and register the factory's GSAs in it.
4. Create templates, owner workflow hooks, project env and schedules **only if they are missing**.
5. Set up the repo: a setup PR that adds CODEOWNERS entries (owners on the factory folder and its `evals/` folder) and
   branch protection on the default branch (PR required, required checks, CODEOWNERS review). When the factory's token
   lacks admin rights, onboarding prints the exact settings for an owner to apply, and reports them as missing.
6. Record the baseline, grant the owners access (§6.2.5), and write the registry document.
7. Render the admission-policy parameters for the factory (§6.2.5).
8. Start the lead and create its heartbeat and weekly schedules.

A re-run creates what is missing and reports everything it left alone. It never updates or deletes an owner-editable
artifact.

`scion-factory retire <slug>` removes the owners' Hub roles (and their IAP access when no other factory needs it),
stops and deletes the factory's agents, pauses its schedules, removes its KSAs, Workload Identity bindings and
admission-policy parameters, and marks the registry document `retired`. It keeps the repo and the warehouse data.

**Clean start.** The running factory is not migrated. Fleet setup retires the legacy control plane (the env-driven
CronJob pair, the old control-KSA binding and the `scion-worker`/`scion-deployer` KSAs) and the old factory's Build
and Eval projects, then the factory is onboarded again with `onboard`. On a platform that has a registry, `provision`
refuses and points to `onboard`.

#### 6.2.4 Identities and model access

- A worker GSA and a deployer GSA in the factory's sandbox, named from the factory slug (shortened with a stable hash
  suffix when a name would exceed 30 characters).
- KSAs `<slug>-worker` and `<slug>-deployer` in the shared runtime namespace, annotated with the factory's GSAs, with
  `roles/iam.workloadIdentityUser` on each GSA for its own KSA only.
- The factory's Scion project defaults to identity mode `assign` with the worker GSA. The deployer GSA is registered
  only in that project.
- One platform control identity: a GSA in the host project on KSA `scion-factory-control`, created once at fleet setup
  and never re-pointed afterwards. Onboarding adds it to the factory's Scion project with the role it needs to read
  agents and manage schedules.
- Agent pods may not use the namespace's `default` KSA (the admission policy rejects it), and fleet setup removes that
  KSA's binding to the host project's agent GSA unless something other than agent pods depends on it.
- One repo credential per factory, scoped to that factory's repo (a fine-grained token until a GitHub App exists
  upstream). Factories that share a monorepo share the repo, but never each other's credential.
- **Model calls bill to the host project.** The Hub-scope `GOOGLE_CLOUD_PROJECT` and every harness's Vertex project
  setting point at the host project. Each factory's worker GSA gets `roles/aiplatform.user` on the host project and no
  model access in its sandbox. Claude is enabled in Model Garden once, in the host project.

#### 6.2.5 Owners, their edit surface, and how isolation holds

- Onboarding gives each owner IAP access to the Hub, a Hub user, and a project role in that factory's Scion project
  only. The role is the narrowest Scion offers that keeps template, hook, env, schedule and agent control, without
  `project.create`, `project.manage` or harness-config writes. Every owner receives agent escalations (§6.2.10).
- **Owner-editable:** role templates (prompts, and the behavior fields of `scion-agent.yaml`), workflow hooks (§6.2.6),
  project env vars that are not reserved, and the factory's schedules.
- **Platform-owned:** identity and Kubernetes fields of templates (service account, namespace, volumes, image), reserved
  env vars (model project, location, telemetry settings), harness configs, GCP service-account registration, safety
  gates, KSAs and IAM, the control plane, the eval rubrics, and every other factory.
- **Enforcement below Scion.** Scion cannot lock individual fields, and live edits are never reverted, so isolation
  cannot depend on owners not editing. A Kubernetes ValidatingAdmissionPolicy in the runtime namespace checks every pod
  labeled `scion.agent=true` against parameters rendered per factory from the registry, and rejects the pod when:
  - its project is not an active factory in the registry (default deny, which also covers any project an owner manages
    to create);
  - its service account is not one of its project's KSAs (the control KSA and `default` are never allowed);
  - its image is outside the platform registry;
  - a reserved env var differs from the platform value;
  - it mounts a volume or path outside its project's own workspace and shared dirs.

  A rejected start fails visibly, the drift recorder flags the edit that caused it, and the owner guide explains it.
- **Isolation tests**, with two factories, through the API, the web UI and direct pod specs: an owner of A cannot list,
  read, message or change B; cannot register a foreign GSA; agents of a project that is not a registered factory cannot
  start; and a template edit pointing A's agent at B's KSA, the control KSA, `default`, a foreign image or B's files is
  rejected at pod admission.
- `docs/project-owner-guide.md` explains what an owner gets, the safety gates and workflow hooks their factory starts
  with, what they can change, that changes are recorded for learning, and what a rejected start means.

#### 6.2.6 Baseline hooks in two tiers

The factory ships a hook set; this is where "how the hooks of that work work" is defined.

- **Safety gates (platform-owned, every harness).** The starting list: no push to the default branch, no force push, no
  `git add -A`/`git clean`/`git reset --hard`, no deploy or IAM command from an agent other than the deployer, and no
  commit that matches the repo's secret patterns. The gates must hold even when a template or the workspace changes
  harness settings, so they are installed where neither can override them (system-level managed harness settings, or a
  command shim in the platform image), and backed by server-side controls where they exist (branch protection, GitHub
  push protection).
- **Workflow hooks (owner-editable, baseline provided).** Carried by the templates and the project pre-start hook: for
  example run the product's tests before a commit, lint before a push, and check the PR description against the issue's
  acceptance checks. Platform needs never live in the project pre-start hook, because an owner's version replaces it.
- Every hook decision writes a verdict event (`allowed`, `blocked` or `flagged`, with the gate or hook ID), which is
  what the hook catch rate counts.

#### 6.2.7 Live-edit protection and drift recording

- **Rule for all platform tooling:** owner-editable artifacts are created if missing and never updated or deleted.
  Platform-owned resources are reconciled as today.
- **Watchdog.** The heartbeat guard resumes a schedule only when the platform paused it (recorded in the registry) or
  Scion paused it (non-empty `statusReason`). An owner's pause stays and is recorded as drift.
- **Baseline.** At onboarding, store a content hash for each owner-editable artifact: each template file, each workflow
  hook, each schedule's cron, message and state, and each project env var.
- **Drift recorder.** Part of the fleet control plane (every 15 minutes). It reads the artifacts through the Hub API,
  updates the registry, and for each change writes one `factory.drift` event: factory, artifact, old and new hash, time
  window, the editor when the Hub exposes it, a reserved-field flag when the edit touches a platform-owned field, and the
  unified diff (secrets redacted, size-capped). It never writes to the Hub.

#### 6.2.8 Fleet control plane

- One `scion-factory-sync` and one `scion-factory-watchdog` CronJob in the shared namespace, driven by the registry
  instead of env. Onboarding never renders factory values into them.
- The sync writes `factory.agent` snapshot events to Cloud Logging (Scion agent ID, slug, factory, template, harness,
  model, issue number, branch, and created, started, stopped and deleted times) instead of Firestore
  `factory_agent_status`. Owners see live agent state in the Hub UI.
- Env-driven single-factory mode stays for installs not yet re-onboarded.

#### 6.2.9 Issue flow: one PR per issue

- **Branch and agents.** For each issue it starts, the lead creates the branch `factory/issue-<N>` from the default
  branch and creates the issue's agents, named `issue-<N>-<role>`, from the roles' templates with the roles' harness and
  model. One writer works on an issue branch at a time; how each agent gets its checkout of the branch (the agent's own
  branch checkout or a worktree) is the builder's choice.
- **PR.** The implementer pushes the branch and opens a PR that says `Closes #N` and lists the issue's acceptance
  checks. The reviewer posts its PASS/FAIL verdict as a PR review comment.
- **Evaluate before the owner sees it.** When the factory deploys a Cloud Run service (`infrastructure.deploy:
  service`), the deployer deploys the PR head as a tagged revision with no traffic. The product evaluator
  (`issue-<N>-evaluator`) starts `run_product_regression` in BigQuery against that revision's URL (§6.3.6), adds checks
  specific to the issue (recorded as ad hoc results), posts the results on the PR, and proposes new golden-set entries
  or probes in `evals/` as part of the PR. UI issues also get `issue-<N>-ui-craftsman` for a design review. With
  `deploy: none`, the evaluator grades the code and tests, and the regression signal comes from CI.
- **Accept.** An owner reviews and merges (merge is acceptance; the issue closes through `Closes #N`). When the owner
  requests changes, the lead re-briefs the issue's agents.
- **After merge.** The deployer rolls the merged commit to live traffic and runs the post-merge checks. The lead
  confirms everything is recorded, then stops and deletes the issue's agents.
- **Shared agents.** The lead stays up and keeps `team.max_wip` issues in flight. The deployer is one agent per factory
  so deploys stay serialized; the lead starts it when needed and stops it when idle.
- **Nobody grades their own work.** Golden sets and probe definitions live in `evals/`, which CODEOWNERS assigns to the
  owners, so a change to them needs an owner's approval; the PR is labeled when it touches `evals/`. BigQuery loads
  `evals/` only from the default branch, so only owner-approved golden sets are ever used. The rubrics that score
  trajectories (§6.3.6) are platform-owned.
- The change lands in the lead and shared-protocol templates, the generalized product-evaluator template (from
  search-evaluator), the ui-craftsman template, and the kit compiler (`scripts/scion_factory.py`). The eval-lead and
  eval-deployer templates and the eval dashboard are retired from new kits.

#### 6.2.10 Escalations

- Agents escalate to every owner of their factory, and each escalation also writes a `factory.escalation` event.
- The platform admin is alerted when an escalation has no owner response within 24 hours.
- This replaces the watchdog's inbox-based PO escalation forwarding, which cannot read owners' inboxes.

#### 6.2.11 Turn on early: raw landing zone

So that no event is lost while Stage 2 is built, Stage 1 already creates:

- the warehouse dataset and a Log Router sink that routes `scion-analytics` and the `factory.*` events into
  date-partitioned raw tables;
- the transcript bucket (§6.3.2), with per-factory prefixes;
- the Cloud Billing export to BigQuery for the billing account of the host project (it does not backfill).

#### 6.2.12 Stage 1 is done when

1. Two factories with different sandboxes run on one platform, each as one Scion project, and onboarding the second
   changed nothing for the first.
2. The isolation tests in §6.2.5 pass, including pod-admission rejections.
3. An owner edits a template and a workflow hook and pauses the heartbeat; after an `onboard` re-run, a sync and a
   watchdog cycle, all three edits remain and three `factory.drift` events exist.
4. One issue runs end to end: branch, PR, reviewer verdict, preview revision, evaluator results on the PR, owner
   merge, live rollout. Afterwards only the lead runs.
5. Model calls of both factories bill to the host project, and `factory.*` and `scion-analytics` events reach the raw
   tables.
6. `retire` removes the owner's access, the KSAs and the policy parameters, and the other factory is unaffected.
7. All existing tests pass, new behavior has tests, and every new script works with `DRY_RUN=1`.

### 6.3 Stage 2 — Everything in BigQuery

#### 6.3.1 What lands in the warehouse

| Data | Source | Path |
|---|---|---|
| Token usage per call | upstream `sciontool` (`scion-analytics`, `agent.usage`) | Cloud Logging → sink. **The only token source:** one usage source per harness, deduplicated on `(producer_namespace, event_id)`. |
| Agent snapshots | fleet sync (`factory.agent`) | Cloud Logging → sink |
| Trajectory events | trajectory emitter (`factory.trajectory`) | Cloud Logging → sink. One event per hook call: session start and end, prompt, model response, tool call with input and output, turn end, errors and retries. Large bodies are stored once in the transcript file and referenced by ID. |
| Full transcripts | trajectory emitter | JSONL per agent session in the transcript bucket, loaded into BigQuery |
| Hook verdicts | safety gates and workflow hooks | Cloud Logging → sink |
| Live edits | drift recorder (`factory.drift`) | Cloud Logging → sink |
| Product eval results | `run_product_regression` (scheduled or agent-started), and evaluators' ad hoc checks (`factory.eval_result`) | The procedure inserts its results directly; ad hoc checks go through Cloud Logging → sink |
| Golden sets and probes | `evals/` on the factory's default branch | Loaded by the GitHub outcomes job after each merge |
| Escalations | agents (`factory.escalation`) | Cloud Logging → sink |
| GitHub outcomes | GitHub outcomes job | BigQuery load: issues, PRs, reviews and review comments, checks, merges, reverts, labels |
| Billing | Cloud Billing export | BigQuery |
| Prices | price table | List price per model and token type, versioned by date |

#### 6.3.2 Transport and redaction

- Small structured events go through Cloud Logging. Full transcripts and large tool outputs go as JSONL files to the
  transcript bucket, so no entry nears the 256 KB limit and logging ingestion stays small.
- Redaction happens in the pod, before anything is written: the repo's secret patterns, common token formats, and
  credential-like values. Each emitter reports how much it redacted in its health events.
- A procedure deletes one factory's or one session's rows and files, for a secret found after the fact.
- **Event integrity.** The factory an event belongs to comes from trusted metadata (the pod labels the platform's
  logging path attaches, or the identity that wrote the event), never from fields inside the payload, because agent-run
  code could write anything there. Metrics that leadership and the harvester rely on use only events attributed this way.
- The emitter never fails or slows a hook: it buffers locally and ships in the background.
- Before building the emitter, inventory what the deployed `sciontool` already writes (log IDs and fields), and add only
  what is missing. If the deployed `sciontool` predates `scion-analytics`, rebuild the images from an upstream commit
  that has it.

#### 6.3.3 Telemetry completeness

- **Contract.** A written list of the events every agent session must produce, with required fields, per event type.
- **Fixtures in CI.** For each harness and version the fleet uses, recorded hook and OTLP payloads prove the mapping:
  one model call gives one usage event with the tokens the harness itself reports, and every tool call gives a start and
  an end. A harness upgrade reaches the fleet only after its fixtures pass.
- **Coverage checks.** Scheduled queries check that every agent that ran has usage and trajectory events, that every
  merged PR has a cost, and the gap rate per harness and version. Coverage below 99% of sessions in a day raises an
  alert.
- **Health.** Upstream `telemetry.health` events and the emitter's own health (backlog, drops, redactions) are
  monitored.
- **Canary.** A platform-owned canary factory on a small test repo runs one scripted issue every week, end to end, and
  checks that every expected row reached the warehouse.

#### 6.3.4 Work context

- **Issue and PR:** from the agent name (`issue-<N>-<role>`), the branch (`factory/issue-<N>`) and the PR that closes
  the issue.
- **Factory and role:** from the registry and the agent's template. **Harness and model:** from `agent.usage`.
- **Template version:** the artifact hashes in force when the agent started, so outcomes can be compared before and
  after a live edit.
- **Shared agents** (lead, deployer, weekly analyst): reported as overhead per factory, and also apportioned to issues
  in proportion to each issue's direct cost in the same week. Reports show direct and apportioned cost separately.

#### 6.3.5 Cost and budgets

- Token cost = tokens by type × the dated list price for that model. Where a harness reports its own cost, keep both
  and show the difference.
- Monthly, the warehouse total for the fleet is compared with the host project's Vertex AI billing for the same
  period. The target gap is within 10%; a larger gap raises an alert and is never silently corrected.
- Each factory's month-to-date model spend is checked against its `infrastructure.budget_usd` (the factory's monthly
  ceiling), with alerts at 50%, 80% and 100%. Sandbox Cloud Billing budgets for product infrastructure stay as today.
- Pod compute cost is out of scope for the first cut; the column is reserved.

#### 6.3.6 Evals, computed in BigQuery

**Metrics (views and scheduled queries).** The unit of work is the PR that closes an issue.

| Metric | Definition |
|---|---|
| Cost per PR and per issue | Direct token cost of the issue's agents, plus apportioned overhead (shown separately). |
| Task completion | The PR was merged by an owner and closed its issue. |
| Harness × model scorecard | Per harness, model and role: issues attempted, completion rate, cost per verified merged PR, median cycle time, and change size (files and lines), with sample size shown and a flag below a minimum sample. |
| First-pass acceptance | Share of merged PRs with no owner change request, required checks green on the first run, and the evaluator's first result passing. |
| Review rounds | Owner change requests and rework pushes per PR. |
| Cycle time | Issue start to PR opened, and PR opened to merge. |
| 7-day survival | Share of merged PRs with no revert and no linked regression issue within 7 days. |
| Thrash | Per issue: tool errors followed by retries of the same tool, repeated identical tool calls, compactions, watchdog rescues. |
| Hook catch rate | Per issue: verdicts that blocked or flagged an action, by gate and hook. |
| Product quality | From `factory.eval_result`: each factory's primary metric (for example NDCG@10), latency percentiles and requirement status, per PR and over time. |

"Verified merged" means merged and survived 7 days.

**Eval procedures.** Two BigQuery stored procedures hold all eval logic, so a scheduled run and an agent-started run
are the same run:

- `score_trajectories(factory, scope)` scores the trajectories and PRs in scope that are not scored yet. It calls Gemini
  (host project) through `AI.GENERATE` with an output schema, so each platform-owned rubric returns a score and a
  reason, and it inserts the judgments with the rubric version and the judge model. The first rubrics: protocol
  adherence (status at the end of each turn, delivered messages, evidence on the issue), plan quality against the
  issue's acceptance checks, review depth (did the reviewer check what the issue asked), and the root cause of churned
  or reverted PRs. Each rubric is checked against a small hand-labeled sample before its scores feed the harvester.
  Every merged PR is scored; sessions are sampled to bound cost.
- `run_product_regression(factory, target, scope)` serves factories with a deployed service. It sends each golden-set
  query and probe in scope to the target (the live service, or a PR's preview revision) through a BigQuery remote
  function, computes the factory's primary metric (for example NDCG@10 and MRR@10), latency percentiles and requirement
  status in SQL, uses `AI.GENERATE` only for checks SQL cannot express (such as whether an answer is relevant), and
  inserts one run with its results.

Golden sets and probes are data, not code: JSONL files in `evals/` with a fixed schema, loaded from the default branch
after each merge. Product checks that need code (multi-step UI flows, for example) stay in CI, whose results arrive
through the GitHub outcomes job. No factory code runs with a platform identity.

**The probe helper.** BigQuery cannot make HTTP calls by itself. The remote function behind `run_product_regression` is
a small platform-owned Cloud Run function that forwards each probe and returns status, latency and body. Each factory has
its own BigQuery connection, whose service account may invoke only that factory's services, and the function refuses
any target that is not one of the factory's registered URLs.

**Who starts an eval run.**

| Trigger | What runs |
|---|---|
| Daily BigQuery scheduled query | `score_trajectories` for everything new, and `run_product_regression` against each live service |
| The lead, for each PR | The evaluator runs `run_product_regression` against the PR's preview revision (§6.2.9) |
| The lead, after each merge and rollout | `run_product_regression` against the live service |
| Weekly schedule (owners can change it) | The factory analyst (§6.3.7) |
| An owner, any time | A Hub message to the lead, or an `/eval` comment on an issue or PR, starts the evaluator or the analyst with a scope |

Every run is recorded with its trigger, who asked, its scope, the rubric version and its results, and runs cannot be
deleted. Each run also scores whatever in its factory is not scored yet, so fleet comparisons and the harvester see
every merged PR, whoever started what.

**Report.** A Looker Studio report on the views replaces the eval dashboard: a fleet page for the platform admin and
leadership, and a page per factory.

#### 6.3.7 Factory analyst (weekly and on demand)

- A weekly schedule, or an owner's request, asks the lead to start an analyst (`analyst-<year>w<week>`, or
  `analyst-<date>-<n>` on demand) from the factory-analyst template. The analyst reads its factory's warehouse rows
  (views, judgments and trajectories), can start `score_trajectories` for its scope, writes a report as a GitHub issue
  for the owners with concrete improvements (template or hook changes, each with its evidence query), and then the lead
  deletes it.
- Its reports are also an input for the harvester.

#### 6.3.8 Access

- Row-level access by factory: platform admins see every row; each factory's owners and its worker GSA (for the
  analyst) see only that factory's rows, content included.
- Leadership sees the fleet through aggregate views without transcript content.
- Raw transcripts and trajectory content are kept for 365 days; metrics and aggregates are kept indefinitely.

#### 6.3.9 Stage 2 is done when

1. For one real PR, its cost matches a hand calculation from the raw `agent.usage` rows, and the monthly reconciliation
   and budget alerts run.
2. Every metric above, the rubric judgments and the product-quality series return rows for both test factories.
3. Coverage checks, emitter health and the weekly canary run, and a deliberately broken emitter raises an alert.
4. A seeded fake secret in a tool output never reaches the warehouse, and the delete procedure removes a session's rows
   and files.
5. An owner, and their factory's analyst, see only their factory's rows; leadership views contain no transcript text.
6. The weekly analyst opens its report issue and is deleted afterwards, and an owner's `/eval` comment starts a run.
7. The same procedure gives the same result for the same inputs, whether the schedule or an agent starts it.
8. A probe aimed at a URL that is not one of the factory's registered services is refused.

### 6.4 Stage 3 — Self-learning harvester

- A scheduled Cloud Run Job in the host project, weekly. It runs outside the Hub and needs no Hub authority beyond
  reading.
- For each live edit (`factory.drift`), it compares the factory's metrics and rubric scores before and after the edit,
  and against factories still on the baseline. It proposes a candidate only with at least 10 issues on each side and a
  better completion rate or lower cost per verified merged PR, with no worse 7-day survival. Weekly analyst reports
  add context, never evidence on their own.
- It generalizes each candidate: strips project-specific content (repo names, paths, product terms, people) so the
  change fits `templates/` in `scion-defaults`. An LLM may draft the generalized change; the job checks it with the repo
  secret scan and the test suite.
- It opens one PR on `scion-defaults` per run with the changes and an evidence table (factories, sample sizes, before
  and after metrics, links to the drift events). It never merges.
- Each factory's baseline records the `scion-defaults` commit it was onboarded from, so later runs can compare cohorts.

#### 6.4.1 Stage 3 is done when

1. With seeded data (one edit that clearly helped, one that did not, one with too few issues), the job proposes only
   the first.
2. The PR passes the test suite and the secret scan, and contains no project-specific strings from the seeded factories.
3. A factory onboarded after the PR merges records the new commit in its baseline.

## 7. Left to the builder

- File and module layout, script names, and CLI flag spellings beyond those named here.
- Which Scion permissions make up the owner role, as long as the isolation tests pass.
- How each agent gets its checkout of an issue branch.
- How the trajectory emitter hooks in (the `sciontool` wrapper, harness configs, or an image layer) and how it finds
  each harness's transcript, as long as it is non-blocking and every fleet harness passes its fixtures.
- Exact BigQuery schemas, partitioning, clustering, view SQL, and whether transcripts are loaded or read as external
  tables.
- How owners' IAP access is granted (individual bindings or a managed group).
- How the drift recorder learns who made an edit, if the Hub exposes it.
- How an owner's response to an escalation is detected.
- How the eval procedures get their permissions (caller rights with row-level policies, or authorized routines), as
  long as an agent can only evaluate and read its own factory.
- The remote function's language, batching (`max_batching_rows`) and timeouts, and the JSONL schema for golden sets and
  probes.
- The statistical test the harvester uses, within the minimums above.

## 8. Verification

- Unit tests for every new script and changed behavior: registry, onboarding phases and refusals, create-only writes,
  the watchdog pause rule, drift hashing and redaction, identity naming, admission-policy parameter rendering, event
  parsing, cost math, and coverage queries.
- Rendered-manifest tests for the fleet control plane (no factory values in CronJob env) and policy tests for the
  admission policy (every rejection case in §6.2.5, plus a pod that should pass).
- Emitter fixtures per harness in CI (§6.3.3).
- Procedure tests on a seeded dataset: `score_trajectories` scores only unscored rows and records rubric versions;
  `run_product_regression` computes known metric values from a fixed golden set against a stub service; the remote
  function refuses targets outside the factory's registered URLs.
- A two-factory end-to-end run on the real platform for each stage's done-when list, with what it taught recorded in
  `docs/lessons-learned.md`.
- The secret scan from `AGENTS.md` before every commit.

## 9. Risks and triggers

- **Content volume and cost.** Full trajectories are large. Watch warehouse storage, Cloud Logging ingestion and judge
  cost per factory in the report; sampling and retention are the levers.
- **BigQuery AI and remote-function limits.** `AI.GENERATE` draws on Vertex AI quota (shared, or provisioned
  throughput). Remote functions take only scalar and JSON values, batch rows, and fail on slow endpoints. Long
  transcripts may need trimming to fit the model's context. A failed scheduled query raises an alert.
- **Redaction misses.** A pattern-based redactor will miss something eventually; the delete procedure and access limits
  bound the damage.
- **Judge drift.** Rubric scores move when the judge model changes; judgments record rubric version and model, and
  comparisons stay within one version.
- **Branch protection rights.** Some owners' repos may not let the platform configure protection; onboarding reports it,
  and without it the "nobody grades their own work" rule rests on owner review alone.
- **Usage coverage per harness.** A harness without a declared usage source publishes no tokens; the scorecard shows
  coverage per harness instead of reporting zero cost.
- **Single-Hub ceiling.** Filestore IOPS and Cloud SQL connections cap how many factories one platform carries. Measure
  at 25 and 50 factories before planning federation.
- **Single-instance scheduler.** Heartbeats and weekly analyst runs depend on the in-memory Hub scheduler; the watchdog
  keeps checking them.
- **Lead overhead.** An idle lead on a 15-minute heartbeat still spends model turns; overhead is visible per factory so
  each factory can tune `team.heartbeat_cron`.

## 10. Decisions

### 10.1 Decided by the platform admin (2026-11-05)

1. One branch and one pull request per issue; the owner merges to accept.
2. Everything goes to BigQuery, content included (prompts, responses, tool inputs and outputs, diffs), with secrets
   removed and access limited to platform admins and each factory's own owners.
3. Model calls bill to the host project; per-factory budgets become warehouse alerts.
4. Clean start: the running factory is re-onboarded and its Eval project retired.
5. The Eval factory is removed. The product evaluator and ui-craftsman join the build factory per issue, factory-analyst
   stays as a weekly agent, and eval-lead and eval-deployer retire.
6. Eval runs are driven by factory agents and can be triggered on demand. The scheduled evals (rubric scoring and
   product regression) run inside BigQuery, through the same procedures the agents use.

### 10.2 Calls the author made (please check)

1. Owners cannot change identity, Kubernetes, image or reserved-env settings. A ValidatingAdmissionPolicy enforces
   isolation at pod admission (default deny for any project that is not an active factory), and the owner role is the
   narrowest Scion allows.
2. Hooks come in two tiers: platform-owned safety gates, installed where templates cannot override them and backed by
   branch protection, and owner-editable workflow hooks.
3. Factories may share a monorepo (CODEOWNERS per folder), never a sandbox.
4. Escalations go to every owner of the factory, and the platform admin is alerted after 24 hours without a response.
5. The fleet registry lives in Firestore in the host project; agent status moves from Firestore to `factory.agent`
   events, which changes rule 3 of `AGENTS.md`.
6. Stage 1 already turns on the raw landing zone and the billing export, so nothing is lost before Stage 2.
7. Small events go through Cloud Logging; full transcripts go through a bucket. The factory an event belongs to comes
   from trusted metadata, never from the payload.
8. Coverage target: 99% of sessions a day; the canary runs weekly; scheduled scoring and regression run daily.
9. The first four rubrics in §6.3.6, each checked against a hand-labeled sample.
10. Content and transcripts are kept 365 days. Leadership sees aggregates without transcript text. Besides the owners,
    the factory's own worker identity (which the weekly analyst uses) can read that factory's rows.
11. The drift recorder runs every 15 minutes; the harvester runs weekly and needs at least 10 issues on each side.
12. Shared-agent overhead is apportioned by direct-cost share; the reconciliation target is within 10%; pod compute cost
    is out of scope for the first cut.
13. Branch name `factory/issue-<N>`, agent names `issue-<N>-<role>`, `analyst-<year>w<week>` and
    `analyst-<date>-<n>`.
14. Preview deploys use a tagged Cloud Run revision with no traffic.
15. The heartbeat stays (no webhooks), and harness/model comparisons are observational.
16. All eval logic lives in two BigQuery stored procedures (`score_trajectories`, `run_product_regression`) that the
    schedules and the agents share.
17. Product probes go through one platform-owned remote function, with one BigQuery connection per factory, limited to
    that factory's registered URLs.
18. Golden sets and probes are JSONL data in `evals/`, loaded only from the default branch; product checks that need
    code stay in CI.
19. Eval triggers: a daily schedule, the lead for each PR and after each merge, the weekly analyst, and owners on demand
    (`/eval` comment or a Hub message to the lead).
