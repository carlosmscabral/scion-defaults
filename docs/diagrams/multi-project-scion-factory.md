# Multi-Project Scion Software Factory — Executive Architecture (Text-Only Companion)

> **Companion to:** [`multi-project-scion-factory.html`](multi-project-scion-factory.html)
>
> **Detailed design:** [multi-project factory design spec](../specs/2026-11-04-multi-project-factory-design.md). This
> executive view dates from 2026-10-30; revision 3 of the spec removes the Eval factory, runs every eval in BigQuery,
> and holds the detailed ASCII flows (§6.1).
>
> **Provenance & Scope Notice:** Shared HA Hub, Cloud SQL, Filestore, GKE Autopilot, Vertex AI, and `sciontool` OTLP are **deployed baseline** components; `N`-project scoped KSA delegation, the BigQuery trajectory warehouse, and the Self-Learning Harvester are **proposed** target capabilities.

---

## 1. Executive Thesis

**One shared Scion factory for `N` teams — that learns from every PR.**

Instead of standing up and maintaining a separate Scion deployment per team, **one High-Availability (HA) GKE + Cloud Run Scion stack** hosts `N` isolated, delegated projects:
1. **Amortized Control Plane (`1 → N`):** Platform Admin seeds each new project on the shared HA Hub from the [`scion-defaults`](../../README.md) baseline and delegates scoped **Project Owners (`1..N`)**.
2. **Live Team Adaptation (Drift as Signal):** Delegated owners inspect their agents and adapt lifecycle hooks, prompts, and harness settings **on the fly** in the Hub without overwriting or affecting other teams.
3. **Elastic Multi-Harness Delivery:** GitHub issues and PR reviews spawn ephemeral per-issue worker and reviewer pods on **GKE Autopilot** across multiple harnesses (`Claude Code`, `Antigravity`, `OpenCode`, `Codex`) that verify changes only in that project's isolated GCP sandbox.
4. **Trajectory, Cost & Quality Warehouse:** Structured trajectories stream through **Cloud Logging** to **BigQuery** and join with GitHub PR/CI/revert outcomes to measure **true `$/PR`**, **`$/Issue`**, and **`Harness × Model` cost vs. completion**.
5. **Closed-Loop Self-Learning Defaults:** A **Self-Learning Harvester** correlates live owner hook/prompt diffs with BigQuery performance wins and opens human-reviewed PRs back on `scion-defaults` so **Project `N+1` starts smarter**.

---

## 2. Text-Only Topology & Closed-Loop Flow

Legend:
- `[1]..[10]` — Primary numbered closed-loop path (onboarding -> issue execution -> trajectory eval -> default promotion)
- `(s1)..(s6)` — Supporting operational paths (admin SSO ingress, HA state/workspace mounts, PR push & GitHub outcome joins)

```text
OUTSIDE GOOGLE CLOUD                     GOOGLE CLOUD (Shared Software Factory & Isolated Target Sandboxes)
====================                     ==================================================================

+---------------------------+            +--- Shared HA Control Plane (Factory Host Project - Deployed) ---+
| Platform Admin            |--(s1)----->| Identity-Aware Proxy (Google SSO, scoped access)                |
| provisions Project 1..N   |  onboard   |        | (s2)                                                   |
+---------------------------+  + RBAC    |        v                                                        |
                                         | Scion HA Hub (Cloud Run - hosts Projects 1..N)                  |
+---------------------------+            |        | (s3)                           |                       |
| Project Owners (1..N)     |--[2]------>|        v                                |                       |
| scoped RBAC, live hooks   | hook edits | Cloud SQL (PostgreSQL - RBAC & hashes)  |                       |
+---------------------------+   (->IAP)  |                                         |                       |
                                         | Filestore (NFS RWX - agent workspaces)  |                       |
+---------------------------+            +---------------------------------^-------+-----------------------+
| Product GitHub Repos      |--[3]------>  (into Scion HA Hub)             |       |
| issues, PRs, CI status    |<-(s5)------.                                 | (s4)  | [4]
+---------------------------+            |                                 |       v
      |                                  |  +-- Elastic Agent Compute -----+-------+---+     +-- Frontier Models (Deployed) --------+
      | (s6) merge & revert events       |  | GKE Autopilot Agent Pool                 |--[5]-> Vertex AI & Model APIs              |
      |                                  '--| per-issue pods + sciontool               |     |  Gemini, Claude, multi-model         |
      |                                     | (Claude Code, Agy, OpenCode, Codex)      |     +--------------------------------------+
      |                                     +------------------------------------------+
      |                                                   |                |                 +-- Isolated Sandboxes (Proposed) -----+
      |                                                   |                '-------------[6]-> Project Sandboxes (1..N)             |
      |                                                   |                      rollout     |  isolated GCP, <slug>-deployer KSA   |
      |                                                   | [7] work tags                    +--------------------------------------+
      |                                                   v
      |                                     +-- Self-Learning Loop (Proposed) ---------+     +-- Trajectory & Eval Plane (Proposed) +
+---------------------------+               |                                          |     | Cloud Logging & Router               |
| scion-defaults Repo       |--[1]--------->| (baseline into Scion HA Hub)             |     |  trajectories, OTLP, sink            |
| golden templates & hooks  |  baseline     |                                          |     |                  |                   |
|                           |<-[10]---------| Self-Learning Harvester                  |<-[9]-                  | [8] stream        |
+---------------------------+  upgrade PR   |  Cloud Run Job - drift miner             | eval|                  v                   |
                                            +------------------------------------------+ win | BigQuery Eval Warehouse              |
                                                                                             |  $/PR, $/Issue, harness evals        |
      '------------------------------------------------------------------------------------->|  (joins trajectories + GitHub PRs)   |
                                                                                             +--------------------------------------+
```

---

## 3. Component Inventory (`14` Units Across `7` Boundaries)

### A. Outside Google Cloud (Actors & Git Repositories)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `admin` | **Platform Admin** | `provisions Project 1..N` | Creates isolated Scion projects on the shared Hub and assigns delegated Project Owners without standing up another Scion cluster. | Amortizes one HA control plane across the engineering portfolio instead of maintaining `N` separate Scion deployments. | Baseline + `N`-project CLI |
| `owners` | **Project Owners (`1..N`)** | `scoped RBAC · live hooks` | Inspect provisioned agents for their project and tune lifecycle hooks, prompts, and harness settings on the fly. | Teams adapt agent guardrails at delivery speed while live edits become candidate signals for future factory defaults. | Proposed Delegation |
| `github` | **Product GitHub Repos** | `issues · PRs · CI status` | Holds each team's application code, incoming issues, pull requests, CI checks, merge events, and post-merge reverts. | Anchors agent work to concrete issues and PRs so cost and quality evaluations reflect merged software outcomes. | Baseline + Outcome Sync |
| `defaults` | **`scion-defaults` Repo** | `golden templates & hooks` | Versioned blueprint catalog of agent role templates, harness hooks, verification gates, and starter presets. | Turns hard-won prompt and hook improvements from live teams into reviewable defaults for Project `N+1`. | Baseline + PR Loop |

### B. Shared HA Control Plane (Factory Host GCP Project)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `iap` | **Identity-Aware Proxy** | `Google SSO · scoped access` | Authenticates platform admins and delegated project owners with corporate Google identity before any request reaches the Hub. | Eliminates public unauthenticated ingress while passing verified identity headers into Scion's project RBAC. | Deployed Baseline |
| `hub` | **Scion HA Hub** | `Cloud Run · Projects 1..N` | Single multi-project control plane hosting `N` isolated groves, project-scoped RBAC, templates, hooks, and agent messaging. | One HA deployment serves every onboarded team, avoiding duplicated control planes and operational drift. | Deployed Baseline *(+ `1..N` tenancy)* |
| `sql` | **Cloud SQL** | `PostgreSQL · RBAC & hashes` | Stores multi-project metadata, user delegation grants, agent state, and each project's initial template baseline hash. | Recording the initial baseline hash lets the platform detect intentional owner hook adaptations without overwriting them. | Deployed Baseline *(+ baseline hash)* |
| `nfs` | **Filestore** | `NFS RWX · agent workspaces` | Provides durable `ReadWriteMany` workspaces mounted per project so resumable coordinators keep git worktrees across pod restarts. | Shared HA storage survives Cloud Run or GKE node rescheduling without losing in-progress branches. | Deployed Baseline |

### C. Elastic Agent Compute (Factory Host GCP Project)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `gke` | **GKE Autopilot Agent Pool** | `per-issue pods + sciontool` | Runs ephemeral per-issue worker and reviewer pods across `Claude Code`, `Antigravity`, `OpenCode`, and `Codex` with in-pod `sciontool` telemetry. | Per-issue pods scale to zero when idle and isolate clean cost and trajectory attribution per GitHub issue and PR. | Deployed Baseline *(+ per-issue lifecycle)* |

### D. Frontier Models (Multi-Harness Inference)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `vertex` | **Vertex AI & Model APIs** | `Gemini · Claude · multi-model` | Serves frontier reasoning and coding models across harnesses so tasks can pair different harnesses and models empirically. | Side-by-side model and harness execution enables objective cost-versus-completion benchmarking on real repositories. | Deployed Baseline |

### E. Isolated Sandboxes (Separate Target GCP Projects `1..N`)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `sandboxes` | **Project Sandboxes (`1..N`)** | `isolated GCP · <slug>-deployer` | Proposed per-project target GCP environments bound solely to that project's Kubernetes Service Account via Workload Identity. | Prevents cross-tenant blast radius: Project A's agents cannot touch Project B's cloud services, data, or secrets. | **Proposed** |

### F. Trajectory & Eval Plane (Factory Host GCP Project)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `logs` | **Cloud Logging & Router** | `trajectories · OTLP · sink` | Proposed structured trajectory pipeline capturing tool calls, hook verdicts, and token counts stamped with project, issue, PR, harness, and model. | Centralizes every harness's execution trace without fragile per-container log scraping before streaming to BigQuery. | **Proposed** |
| `bq` | **BigQuery Eval Warehouse** | `$/PR · $/Issue · harness evals` | Proposed analytics warehouse joining structured agent trajectories, model token pricing, and GitHub PR merge and revert outcomes. | Answers executive ROI questions directly: true cost per merged PR, cost per issue, and which `harness × model` wins. | **Proposed** |

### G. Self-Learning Loop (Factory Host GCP Project)

| ID | Component | Configuration / Scope | What It Does | Why This Design | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `harvester` | **Self-Learning Harvester** | `Cloud Run Job · drift miner` | Proposed scheduled job that compares live project hook and prompt edits against baseline hashes and correlates them with BigQuery KPIs. | Closes the meta-factory loop by promoting only adaptations backed by lower cost or higher verified PR completion. | **Proposed** |

---

## 4. Step-by-Step Walkthrough (`13` Presentation Views)

### Slide `1 / 13` (`◆`) — One shared Scion factory for `N` teams — that learns from every PR
- **Summary:** One HA GKE stack hosts `N` delegated projects, measures cost and quality in BigQuery, and upgrades its own defaults.
- **Speaker Notes:**
  - *Verified today:* Cloud Run HA Scion Hub behind IAP, Cloud SQL PostgreSQL, Filestore RWX NFS, GKE Autopilot runtime, Vertex AI models, and in-pod `sciontool` OTLP telemetry.
  - *Proposed next:* `N`-project onboarding CLI with project-scoped RBAC and per-project Workload Identity KSAs, structured trajectory Log Router sink to BigQuery, PR/issue cost & harness scorecards, and the Self-Learning Harvester PR loop.

### Slide `2 / 13` (`Step 1`) — Seed Project `k` on the shared Hub
- **Path:** `scion-defaults Repo` -> `Scion HA Hub` (`baseline`)
- **Summary:** Platform Admin provisions a new isolated project from `scion-defaults` onto the existing HA Hub — zero new control planes.
- **Key Facts:** `Control planes: 1 shared HA` | `Tenancy: Projects 1..N`
- **Speaker Notes:** Instead of deploying a separate Cloud Run + Cloud SQL + Filestore + GKE stack per team, each new project is registered as an isolated Scion project (grove) seeded from the versioned `scion-defaults` baseline.

### Slide `3 / 13` (`Step 2`) — Delegate owners to adapt hooks live
- **Path:** `Project Owners (1..N)` -> `Identity-Aware Proxy` (`hook edits`) -> `Scion HA Hub` -> `Cloud SQL`
- **Summary:** Responsible project owners sign in via IAP with scoped RBAC to inspect agents and tune hooks and prompts on the fly.
- **Key Facts:** `Access scope: Single project` | `Drift policy: Tracked, not clobbered`
- **Speaker Notes:** Cloud SQL records the initial template hash at onboarding. Fleet sync monitors all `N` projects without overwriting intentional hook or prompt adjustments made by delegated owners.

### Slide `4 / 13` (`Step 3`) — Issues & PR reviews enter the Hub
- **Path:** `Product GitHub Repos` -> `Scion HA Hub`
- **Summary:** GitHub issues and review events route to that project's coordinator on the shared Scion Hub.
- **Key Facts:** `Work unit: Issue / PR` | `Attribution: Stamped from start`
- **Speaker Notes:** Anchoring orchestration to explicit GitHub issue and PR identifiers lets every downstream agent turn carry work-item context.

### Slide `5 / 13` (`Step 4`) — Spawn elastic multi-harness pods
- **Path:** `Scion HA Hub` -> `GKE Autopilot Agent Pool` *(mounting `Filestore`)*
- **Summary:** The Hub launches ephemeral per-issue worker and reviewer pods on GKE Autopilot that stop as soon as the task finishes.
- **Key Facts:** `Harnesses: Claude · Agy · OpenCode · Codex` | `Idle pod waste: Scales down`
- **Speaker Notes:** Replaces standing idle role pods per project with on-demand per-issue pods while mounting resumable Filestore RWX workspaces.

### Slide `6 / 13` (`Step 5`) — Execute turns across frontier models
- **Path:** `GKE Autopilot Agent Pool` -> `Vertex AI & Model APIs` (`LLM turns`)
- **Summary:** Agent harnesses run reasoning, coding, and review turns against Vertex AI and frontier model endpoints.
- **Key Facts:** `Telemetry: sciontool OTLP` | `Tokens: Input · output · cache`
- **Speaker Notes:** In-container `sciontool` normalizes native and hook telemetry across harnesses so token and latency accounting uses one schema.

### Slide `7 / 13` (`Step 6`) — Verify in isolated project sandboxes
- **Path:** `GKE Autopilot Agent Pool` -> `Project Sandboxes (1..N)` (`rollout`)
- **Summary:** Per-project Workload Identity deploys and tests strictly inside that team's target GCP project, then opens the GitHub PR.
- **Key Facts:** `Identity: <slug>-deployer KSA` | `Blast radius: 1 sandbox project`
- **Speaker Notes:** Proposed multi-tenant boundary: dedicated Kubernetes Service Accounts per onboarded project ensure Team A's agents never receive IAM grants on Team B's cloud resources.

### Slide `8 / 13` (`Step 7`) — Emit structured trajectories to Logging
- **Path:** `GKE Autopilot Agent Pool` -> `Cloud Logging & Router` (`work tags`)
- **Summary:** Harness hooks and `sciontool` write structured trajectory logs stamped with project, issue, PR, harness, model, and hook verdicts.
- **Key Facts:** `Correlation: project · issue · pr` | `Collection: Cloud Logging`
- **Speaker Notes:** Proposed trajectory schema captures tool calls, retries, verification gate pass/fail, and token counts directly in structured JSON logs.

### Slide `9 / 13` (`Step 8`) — Score cost & quality in BigQuery
- **Path:** `Cloud Logging & Router` -> `BigQuery Eval Warehouse` (`stream`)
- **Summary:** Log Router streams trajectories to BigQuery and joins GitHub outcomes to rank `$/PR`, `$/Issue`, and `Harness × Model` performance.
- **Key Facts:** `Unit economics: $/PR · $/Issue` | `Quality evals: Pass · cycle · survival`
- **Speaker Notes — Proposed Fleet Evaluation Scorecard:**
  1. **True Cost per Merged PR & per Issue:** Total input/output/cached/reasoning token spend across all planning, coding, review, and rework turns tied to an issue or PR.
  2. **`Harness × Model` Scorecard (Cost vs. Completion):** Pareto frontier comparing task completion rate and cost per verified merged PR across harnesses (`Claude Code`, `Antigravity`, `OpenCode`, `Codex`) and models.
  3. **First-Pass CI & Review Acceptance Rate:** Share of agent PRs passing automated gates and review without multi-round thrash.
  4. **7-Day Post-Merge Survival (Defect Escape Rate):** Share of merged agent PRs that survive 7 days without revert commits, rollback, or hotfix issues.
  5. **Trajectory Thrash & Hook Catch Rate:** Retry loops, blocked unsafe commands, and pre-push hook catches per trajectory.

### Slide `10 / 13` (`Step 9`) — Mine winning live adaptations
- **Path:** `BigQuery Eval Warehouse` -> `Self-Learning Harvester` (`eval winners`)
- **Summary:** The Self-Learning Harvester correlates live owner hook and prompt diffs against BigQuery cost and completion gains.
- **Key Facts:** `Signal: Live hook & prompt diffs` | `Filter: Statistically better KPIs`
- **Speaker Notes:** Proposed scheduled Cloud Run Job inspects what delegated owners changed in high-performing projects and filters out noisy one-off edits.

### Slide `11 / 13` (`Step 10`) — Promote new defaults via pull request
- **Path:** `Self-Learning Harvester` -> `scion-defaults Repo` (`upgrade PR`)
- **Summary:** Verified improvements open an evidence-backed PR on `scion-defaults` so Project `N+1` launches with the upgraded factory baseline.
- **Key Facts:** `Governance: Human-reviewed PR` | `Compounding: Project N+1 starts smarter`
- **Speaker Notes:** Keeps a human platform owner in the loop before new hooks or templates become the organization's default baseline.

### Slide `12 / 13` (`·`) — Guardrails, state & outcome joins
- **Paths:** `s1` (`Platform Admin -> IAP`), `s2` (`IAP -> Hub`), `s3` (`Hub -> Cloud SQL`), `s4` (`GKE -> Filestore`), `s5` (`GKE -> GitHub`), `s6` (`GitHub -> BigQuery`)
- **Summary:** IAP gates every human login, Cloud SQL and Filestore persist HA state, and GitHub outcomes ground BigQuery evals.
- **Key Facts:** `Ingress: Zero anonymous routes` | `State: Cloud SQL + Filestore`
- **Speaker Notes:** Supporting operational paths shown dashed: Platform Admin onboarding via IAP, Hub catalog/hash storage in Cloud SQL, RWX workspaces on Filestore, agent PR creation, and GitHub merge/CI ingestion into BigQuery.

### Slide `13 / 13` (`◆`) — Compounding software factory economics
- **Summary:** Amortize one HA GKE platform across `N` teams, measure true `$/PR` by harness and model, and compound every team's best hooks.
- **Key Facts:** `Platform cost: Shared 1 → N` | `North star: $ / verified merged PR`
- **Speaker Notes — Executive Discussion Takeaways:**
  1. Immediate infrastructure and operational leverage from **1 shared HA Scion deployment** serving `N` projects.
  2. High team autonomy and safety via **delegated Project Owners** and **per-project Workload Identity sandboxes**.
  3. Objective model and harness procurement decisions backed by **BigQuery `$/PR` and completion scorecards**.
  4. Compounding engineering quality as winning live hook/prompt adaptations flow back into **`scion-defaults`**.

---

## 5. Connector Reference Table (`16` Edges)

| Edge | Type | From | To | Wire Label | Purpose |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `e1` (`1`) | Solid | `scion-defaults Repo` | `Scion HA Hub` | `baseline` | Seeds Project `k` from the golden template and hook catalog |
| `e2` (`2`) | Solid | `Project Owners (1..N)` | `Identity-Aware Proxy` | `hook edits` | Authenticated project-scoped owner access to inspect agents & edit hooks live |
| `e3` (`3`) | Solid | `Product GitHub Repos` | `Scion HA Hub` | *(unlabeled)* | Dispatches GitHub issue and PR review events to the project coordinator |
| `e4` (`4`) | Solid | `Scion HA Hub` | `GKE Autopilot Agent Pool` | *(unlabeled)* | Schedules ephemeral per-issue multi-harness worker and reviewer pods |
| `e5` (`5`) | Solid | `GKE Autopilot Agent Pool` | `Vertex AI & Model APIs` | `LLM turns` | Executes reasoning, coding, and tool turns across frontier models |
| `e6` (`6`) | Solid | `GKE Autopilot Agent Pool` | `Project Sandboxes (1..N)` | `rollout` | Deploys and runs integration/smoke verification inside the team's isolated GCP project |
| `e7` (`7`) | Solid | `GKE Autopilot Agent Pool` | `Cloud Logging & Router` | `work tags` | Emits OTLP + structured hook trajectories stamped with `project`, `issue`, `pr`, `harness`, `model` |
| `e8` (`8`) | Solid | `Cloud Logging & Router` | `BigQuery Eval Warehouse` | `stream` | Routes structured trajectory logs into BigQuery via Log Router sink |
| `e9` (`9`) | Solid | `BigQuery Eval Warehouse` | `Self-Learning Harvester` | `eval winners` | Supplies statistically significant cost and completion KPI improvements |
| `e10` (`10`) | Solid | `Self-Learning Harvester` | `scion-defaults Repo` | `upgrade PR` | Opens human-reviewed PR promoting winning hooks/prompts to the default catalog |
| `s1` | Dashed | `Platform Admin` | `Identity-Aware Proxy` | `onboard + RBAC` | Admin SSO login to provision Project `k` and delegate Project Owner RBAC |
| `s2` | Dashed | `Identity-Aware Proxy` | `Scion HA Hub` | *(unlabeled)* | Forwards verified Google identity headers into Scion Hub RBAC |
| `s3` | Dashed | `Scion HA Hub` | `Cloud SQL` | *(unlabeled)* | Persists project catalog, RBAC bindings, and initial template baseline hashes |
| `s4` | Dashed | `GKE Autopilot Agent Pool` | `Filestore` | *(unlabeled)* | Mounts durable `ReadWriteMany` NFS workspaces across pods |
| `s5` | Dashed | `GKE Autopilot Agent Pool` | `Product GitHub Repos` | *(unlabeled)* | Pushes branches, commits, and pull requests back to the product repository |
| `s6` | Dashed | `Product GitHub Repos` | `BigQuery Eval Warehouse` | `merge & revert events` | Syncs PR merge, CI check, and post-merge revert outcomes for cost/quality joins |
