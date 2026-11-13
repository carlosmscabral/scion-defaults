# Setup runbook: a new factory from scratch

This builds the same setup as the original factory: a self-hosted Scion Hub on a GCE VM, a sandbox project, a build
project and an eval project, heartbeats, and the agent-status sync. Plan for half a day; most of it is waiting on
builds and IAM.

Each step ends with a **check**. Don't go on until it passes.

## 0. What you need

- Two GCP projects with billing: `HUB_PROJECT` (the Hub VM and its IAP proxy) and `SANDBOX_PROJECT` (everything the
  agents build and deploy). Keeping them apart means agents can't damage the Hub.
- A GitHub repository (`GITHUB_REPO`) and a personal access token with repo scope (contents, issues, pull requests).
  Every agent acts as the token's owner (L14).
- Locally: `gcloud`, `gh`, `python3`, `bash`. Owner rights on the sandbox; rights to create a VM and an IAP proxy in
  the Hub project.
- A backlog: issues with acceptance criteria, labeled with the build project's slug and `wave-1` ... `wave-N`; a
  `knowledge-plane/INDEX.md` (or similar) in the repo that points to the specs and ADRs. The agents are only as good as
  these issues.

## 1. Configure

```bash
cd docs/scion
cp config/factory.env.example config/factory.env
$EDITOR config/factory.env         # git-ignored; never commit it
```

**Check:** `./scripts/scion-factory compile presets/psearch.factory.yaml --out /tmp/kit && scripts/render-templates.py --src /tmp/kit/templates --out /tmp/tpl` finishes without "missing values".

## 2. Hub VM and Scion server

Follow Scion's own guides for the VM and the server:
[Hub Setup on GCE](https://github.com/GoogleCloudPlatform/scion/blob/main/docs-site/src/content/docs/hosted/single-node/hub-setup-gce.md)
and [Authentication](https://github.com/GoogleCloudPlatform/scion/blob/main/docs-site/src/content/docs/hosted/single-node/auth.md).
What this factory used (deployed via Scion's `scripts/single-node-vm/deploy.sh`):

- VM `e2-standard-4`, 200 GB disk, Ubuntu 22.04, with a dedicated service account (scope `cloud-platform`). In
  `HUB_PROJECT` that SA has `roles/aiplatform.user` (all model calls), `roles/logging.logWriter`,
  `roles/monitoring.metricWriter` and `roles/cloudtrace.agent` (telemetry).
- Built with `deploy.sh`: `machine_size: small`, `disk_size_gb: 200`, `admin_email: $PO_EMAIL`,
  `release_channel: nightly`, `update_policy: notify` (use `notify` rather than `auto` so Scion updates do not restart the Hub unannounced or overwrite provisioners; see [operations-runbook.md](operations-runbook.md#after-a-scion-upgrade)), `container_images.source: build`.
- Docker, git, `sqlite3` and python3 on the VM; the Hub runs as OS user `HUB_OS_USER`, which owns `~/.scion` and can
  use Docker.
- systemd unit `scion-hub.service` running
  `scion server start --foreground --hosted --enable-hub --enable-web --web-port 8080 --enable-runtime-broker`.
- IAP in front of the web UI (`server.auth.mode: proxy`), and `server.hub.agent_endpoint` set to the VM's internal
  address so agents reach the Hub directly.
- `~/.scion/settings.yaml` as in [`config/hub-settings.example.yaml`](../config/hub-settings.example.yaml): every harness
  on Vertex AI with the VM's identity, the PO in `admin_emails`.

**Check:** you can sign in to the web UI through IAP as the PO, `scripts/run-on-hub.sh scripts/scion-admin.py projects`
lists the projects, and `scripts/run-on-hub.sh scripts/scion-admin.py experiments` succeeds (exercising the admin JWT minting path).
To add a factory to a Hub that already runs, skip the VM setup and run the read-only preflight instead:
`scripts/run-on-hub.sh scripts/hub-preflight.py` (health, admin user, images, existing projects, and the merged
`FACTORY_PROJECTS` that keeps other factories covered when you install the watchdog and the sync in step 10).

## 3. Hub experiments

Turn off the beta terminal experiment, which breaks the web terminal button (L2):

```bash
scripts/run-on-hub.sh scripts/scion-admin.py experiments --set web.terminal_workspace=false
```

**Check:** `scripts/run-on-hub.sh scripts/scion-admin.py experiments` shows `web.terminal_workspace enabled=False`.

## 4. Sandbox

```bash
DRY_RUN=1 scripts/setup-sandbox.sh --with-bigquery     # read the plan
read -rs GITHUB_TOKEN && export GITHUB_TOKEN           # paste the PAT; not echoed, not in history
scripts/setup-sandbox.sh --with-bigquery
unset GITHUB_TOKEN
```

Grant up front everything the backlog will obviously need (runtime identities, data stores, secrets): an IAM decision
that waits for the PO stalls the factory for hours (L17).

The factory control plane (agent status and evaluation runs) lives in serverless Firestore; the script creates the
database and its indexes (see [`config/firestore/README.md`](../config/firestore/README.md)). Add `--with-spanner`
only if the product itself has a requirement that Firestore or BigQuery can't meet.
When the factory has nothing to deploy (`infrastructure.deploy: none`), `scripts/setup-sandbox.sh` skips the deployer
and runtime identities; leave `DEPLOYER_SA_NAME` and `RUNTIME_SA_NAME` empty in `factory.env`.

**Check:** run it again; every step reports "exists" and nothing new is created.

## 5. GitHub

```bash
gh label create "$BUILD_PROJECT" -R "$GITHUB_REPO" --description "build factory backlog" --force
gh label create "$EVAL_PROJECT" -R "$GITHUB_REPO" --description "eval factory backlog" --force
for n in 1 2 3 4 5; do gh label create "wave-$n" -R "$GITHUB_REPO" --force; done
```

(Run these with the values from `factory.env`, e.g. after `set -a; . config/factory.env; set +a`.)

Set up branch protection rulesets on the repository to prevent accidental force-pushes or deletions on active factory branches:

```bash
gh api -X POST repos/$GITHUB_REPO/rulesets \
  --input - << 'EOF'
{
  "name": "factory-branch-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": [
        "refs/heads/main",
        "refs/heads/eval-dashboard"
      ],
      "exclude": []
    }
  },
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"}
  ]
}
EOF
```

Store the PAT in the Hub as secret `GITHUB_TOKEN`. Grant `repo` + `workflow` on a classic PAT (or `Contents`, `Issues`,
`Pull requests`, and `Workflows` read/write on a fine-grained PAT) when issues modify `.github/workflows/*`; omitting
`workflow` scope causes GitHub to reject `.github/workflows/*` pushes, in which case agents save a reviewed patch under
`/scion-volumes/scratchpad/pending-<N>/` and open a `status:po-decision` issue while continuing other work (L31). Note: Hub secrets default to injection mode `as_needed`, which skips injecting them into container environments unless explicitly declared. To ensure agents receive the token, set injection mode to **Always** (C2):
- In Web UI: Project → Settings → Secrets → New Secret (`GITHUB_TOKEN`, Injection mode: **Always**).
- Or via CLI:
  ```bash
  scion hub secret set --project="$BUILD_PROJECT" GITHUB_TOKEN "$GITHUB_PAT"
  scion hub secret update --project="$BUILD_PROJECT" GITHUB_TOKEN --injection-mode always
  scion hub secret set --project="$EVAL_PROJECT" GITHUB_TOKEN "$GITHUB_PAT"
  scion hub secret update --project="$EVAL_PROJECT" GITHUB_TOKEN --injection-mode always
  ```

**Check:** `gh api repos/$GITHUB_REPO/rulesets` lists `factory-branch-protection`, and after step 8, `gh auth status` and `git ls-remote origin` succeed inside an agent
(`docker exec -u scion <project>--lead sh -c 'cd /workspace && gh auth status && git ls-remote origin | head -1'`).

## 6. Hub projects and the deployer identity

Create the two Hub-managed projects in the web UI, named `BUILD_PROJECT` and `EVAL_PROJECT`. Without a git remote they
get one shared folder each, `~/.scion/projects/<slug>`, mounted as `/workspace` in all their agents (L8).
*(Alternative: create git-backed projects with `gitRemote`, `workspaceMode: "shared"`, and `githubToken`, which clones automatically and sets up branch tracking).*

Make each folder a checkout of the repository on its branch, as the Hub OS user on the VM (the variables are your
`factory.env` values; on the VM, type them in or export them first):

```bash
# 1. Build project ($BUILD_PROJECT, tracking $BUILD_BRANCH / main)
cd ~/.scion/projects/$BUILD_PROJECT && git init -b $BUILD_BRANCH && git remote add origin https://github.com/$GITHUB_REPO.git
git -c credential.helper='!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' fetch origin
git checkout -B $BUILD_BRANCH origin/$BUILD_BRANCH      # skip for an empty repository
mkdir -p .git/info && printf '.scion/\n.agents/\nscratchpad/\n*.env\n.env*\n' >> .git/info/exclude

# 2. Evaluation project ($EVAL_PROJECT, initialized as an independent orphan trunk with no shared ancestry)
cd ~/.scion/projects/$EVAL_PROJECT && git init && git remote add origin https://github.com/$GITHUB_REPO.git
git -c credential.helper='!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' fetch origin
if git ls-remote --exit-code --heads origin "$EVAL_BRANCH" >/dev/null 2>&1; then
  git checkout -B "$EVAL_BRANCH" "origin/$EVAL_BRANCH"
else
  git switch --orphan "$EVAL_BRANCH"
  git commit --allow-empty -m "chore(eval): initialize independent $EVAL_BRANCH orphan trunk"
  git -c credential.helper='!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' push -u origin "$EVAL_BRANCH"
fi
mkdir -p .git/info && printf '.scion/\n.agents/\nscratchpad/\n*.env\n.env*\n' >> .git/info/exclude
```
*(Writing to `.git/info/exclude` instead of `.gitignore` keeps the working tree clean and prevents permanent untracked/dirty conflicts between agents).*

> [!NOTE]
> `$EVAL_BRANCH` is an **orphan branch** (`--orphan`): it shares no commit ancestry with `$BUILD_BRANCH` (`main`), so `git merge` and GitHub pull requests refuse to merge `$EVAL_BRANCH` into `$BUILD_BRANCH` (or vice versa), and the evaluation workspace never carries product application code (L25).

Register the deployer identity in each project (via Web UI Project Settings → Service Accounts, or from a CLI context linked to that project), so deploy agents can run
as it (Scion `assign` mode):

```bash
scion project service-accounts add "$DEPLOYER_SA_NAME@$SANDBOX_PROJECT.iam.gserviceaccount.com" \
  --gcp-project "$SANDBOX_PROJECT" --name "Sandbox deployer"
scion project service-accounts list          # note the ID; `verify <id>` re-checks impersonation
```

**Check:** `scion project service-accounts list` shows the SA as verified in both projects. Inside each project folder, `git status -sb` tracks its respective branch, and `git merge-base "origin/$BUILD_BRANCH" "origin/$EVAL_BRANCH"` exits with code 1 (proving independent orphan trunks with no shared ancestry).

### Shared directories: scratchpad and knowledge plane

Give both projects two shared directories. Every agent of a project mounts them, whatever the workspace mode, and the
project's **Files** view shows them as tabs next to `workspace`:

| Name | Mount in every agent | Mode | Holds |
|---|---|---|---|
| `scratchpad` | `/scion-volumes/scratchpad` | read-write | briefs, notes and hand-offs that don't belong in Git |
| `knowledge-plane` | `/scion-volumes/knowledge-plane` | read-only for agents | a copy of the build project's `knowledge-plane/`, refreshed every 5 minutes by the watchdog (step 10) |

```bash
scripts/run-on-hub.sh scripts/scion-admin.py shared-dirs            # plan (BUILD_PROJECT and EVAL_PROJECT)
scripts/run-on-hub.sh scripts/scion-admin.py shared-dirs --apply
```

(Same as Project → Settings → **Shared Directories** in the web UI. `scion shared-dir create` edits a local project's
settings file, not a Hub project.) Agents get the mounts when their container is created, so do this before step 8 or
restart the agents afterwards.

**Check:** a second run prints `ok (scratchpad, knowledge-plane)` for both projects.

## 7. Telemetry & Harness Hooks (before agents start)

Configure end-to-end OpenTelemetry (Cloud Logging, Cloud Monitoring, Cloud Trace) and ensure harness hooks run cleanly with sub-second latency (L6):

```bash
# 1. Provision GCP telemetry infrastructure, IAM roles, service account keys, and Hub config
DRY_RUN=1 scripts/setup-telemetry.sh
scripts/setup-telemetry.sh

# 2. Patch sciontool binary wrapper across containers and base images
scripts/patch-sciontool-telemetry.sh

# 3. Configure Antigravity provisioners and hooks for clean telemetry and Gemini 4 Argon
scripts/run-on-hub.sh scripts/patch-antigravity-hooks.py --apply
scripts/run-on-hub.sh scripts/patch-antigravity-hooks.py          # check
```

- `scripts/setup-telemetry.sh` enables `cloudtrace`, `monitoring`, and `logging` APIs, provisions `scion-telemetry-writer` service account credentials, assigns Hub VM SA telemetry viewer/writer roles, and configures `~/.scion/hub.env` and `~/.scion/settings.yaml`.
- `scripts/patch-sciontool-telemetry.sh` inserts `/usr/local/bin/sciontool` wrapper delegating to `sciontool.real` with GCP project credentials, allowing PID 1 to bind loopback OTLP ports `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP).
- `scripts/patch-antigravity-hooks.py --apply` patches all `provision.py` files, `.agents/hooks.json` copies, stages `30-project-custom` across agent homes, and ensures clean `sciontool hook --dialect=antigravity` calls and Gemini 4 Argon model configurations.

**Check:** `scripts/run-on-hub.sh scripts/patch-antigravity-hooks.py` exits 0 with `no drift`, all containers listen on port 4317 (`sudo docker exec <container> ss -tlnp`), and `curl -s http://localhost:8080/api/v1/metrics/dashboard` returns HTTP 200.

## 8. Templates and agents

```bash
scripts/push-templates.py --project "$BUILD_PROJECT" --templates /tmp/kit/templates --roles lead,backend,frontend,reviewer,deployer          # dry run
scripts/push-templates.py --project "$BUILD_PROJECT" --templates /tmp/kit/templates --roles lead,backend,frontend,reviewer,deployer --apply
scripts/push-templates.py --project "$EVAL_PROJECT" --templates /tmp/kit/templates \
  --roles eval-lead,factory-analyst,search-evaluator,ui-craftsman,eval-deployer --apply
```

([templates/README.md](../templates/README.md#uploading-to-the-hub) explains what it does; reviewer uses Gemini 4 Argon via `model: google/gemini-4-argon`, backend uses `google-vertex/gemini-4-argon`, and all Antigravity agents use `Gemini 4 Argon (High)`; L26). Then start the two leads as the admin; each lead starts its own team at its first wake-up:

```bash
scion start lead -t lead --project="$BUILD_PROJECT" "Read your instructions, then run your wake-up steps."
scion start eval-lead -t eval-lead --project="$EVAL_PROJECT" "Read your instructions, then run your wake-up steps."
```

Every agent a lead starts is its child in Scion (ancestry `admin -> lead -> backend`): the project's tree view shows
the lead on top of its team, and the Hub sends each teammate's notifications to the lead instead of the admin (L5).
The leads start the deployers with `--service-account <id>` (the ID from step 6); that step is not yet verified
end to end, so check that the deployers run as the deployer service account.

**Check:** `scripts/run-on-hub.sh scripts/factory-status.sh --panes 10` shows 10 running agents, each terminal at a
prompt, the OpenCode one on "Gemini 4 Argon Vertex" (L3, L26), Antigravity ones on "Gemini 4 Argon · high" (L26), and Hermes on "google/gemini-4-argon" (L26).

## 9. Heartbeats

Create them as the admin, so they keep working if a lead is recreated (L11):

```bash
hb="$(scripts/render-templates.py --file config/heartbeat-message.txt)"
scripts/run-on-hub.sh scripts/scion-admin.py schedule create --project "$BUILD_PROJECT" \
  --name "$BUILD_PROJECT-lead-heartbeat" --cron "*/15 * * * *" --agent lead --text "$hb"
scripts/run-on-hub.sh scripts/scion-admin.py schedule create --project "$EVAL_PROJECT" \
  --name "$EVAL_PROJECT-lead-heartbeat" --cron "*/15 * * * *" --agent eval-lead --text "$hb"
```
*(Note: on re-run, `schedule create` returns HTTP 409 if a schedule by that name already exists; use `schedule update` instead).*

**Check:** after the next quarter hour, `schedule list` shows `runs=1 errors=0`, and the leads' terminals show they
ran the heartbeat steps.

## 10. Agent-status sync, backups, and watchdog

Install the Hub -> Firestore status sync:

```bash
scripts/install-sync-cron.sh --dry-run      # read what it will do
scripts/install-sync-cron.sh
```

Give the evaluation agents the Firestore client: copy [`scripts/factory_store.py`](../scripts/factory_store.py) to
`tools/factory_store.py` in the evaluation workspace (compiled kits ship it in `eval-workspace/tools/`).

Install daily consistent Hub backups and cron:

```bash
scripts/run-on-hub.sh scripts/backup-hub.sh --install-cron
```

Install the watchdog daemon (every 5 minutes):

```bash
scripts/run-on-hub.sh scripts/factory-watchdog.py --install-cron --apply
```

**Check:** `crontab -l` on the Hub VM lists `sync_factory_agents.py`, `backup-hub.sh`, and `factory-watchdog.py`. The sync first run prints `synced 10 agents`, `~/.scion/factory-sync.log` gains a line every minute, `~/backups` contains initial database and state archives, and `~/.scion/watchdog-state.json` contains active agent bookmarks. `~/.scion/watchdog.log` shows `Check 5 (Shared Knowledge Plane): <project>: synced` for both projects, and `cat /scion-volumes/knowledge-plane/INDEX.md` works inside an eval agent.

## 11. Push alerts and billing budgets

Configure Cloud Monitoring email notifications for PO escalations and set monthly GCP billing budgets ($150 on Hub, $150 on Sandbox):

```bash
DRY_RUN=1 scripts/setup-alerts-and-budgets.sh   # preview actions
scripts/setup-alerts-and-budgets.sh             # apply
```

**Check:** `gcloud beta monitoring channels list --project=$HUB_PROJECT` lists the email channel for `$ADMIN_EMAIL`, `gcloud alpha monitoring policies list --project=$HUB_PROJECT` shows policy `Scion Factory Watchdog & PO Escalation Alerts` enabled, and `gcloud billing budgets list --billing-account=$BA` lists the $150 budgets for both projects.

## 12. Start the work

Tell each lead to go, with the rules of autonomy spelled out:

```bash
scripts/run-on-hub.sh scripts/scion-admin.py message --project "$BUILD_PROJECT" --agent lead --text \
  "PO -> lead: GO. Work the backlog on your own in wave order, at most 2 issues in flight, evidence on every issue. Escalate to me only IAM, cost or scope decisions."
```

**Check:** within one heartbeat the lead comments a plan on the first issue and briefs a worker. From here on, follow
[operations-runbook.md](operations-runbook.md).

## Several factories in one monorepo

Use this when one repository holds several products (`src/project1`, `src/project2`, …) and each product gets its own
factory (a build team and an eval team) on the same Hub. Every agent clones the whole repository, starts its harness
in its product folder, is named after its factory (`project1-lead`, `project1-eval-lead`), and reads its own product's
knowledge plane. Steps 0–5, 7 and 11 above apply once per Hub; this section replaces steps 6, 8, 9 and the watchdog part of 10 for each
factory. Design and evidence: [plans/2026-10-22-monorepo-factories.md](plans/2026-10-22-monorepo-factories.md),
[L28](lessons-learned.md#l28-several-factories-in-one-monorepo-names-start-folders-and-what-the-e2e-showed).

| Per factory | Example for `project1` |
|---|---|
| Hub projects | `project1` (branch `main`), `project1-eval` (branch `eval-dashboard`) |
| Agent names | `project1-lead`, `project1-backend`, …, `project1-eval-lead`, … (templates keep the role names) |
| Start folder | `/workspace/src/project1` in every agent of both projects |
| Knowledge plane | `src/project1/knowledge-plane/` in the build checkout, copied read-only to `/scion-volumes/knowledge-plane` in both projects |
| Scratchpad | `/scion-volumes/scratchpad`, one per Hub project |

### M1. Repository layout

- Each product keeps its knowledge plane in its folder: `src/<product>/knowledge-plane/INDEX.md`.
- The eval trunk is **one** orphan branch (`EVAL_BRANCH`, never merged with `main`, L25) that mirrors the monorepo
  layout: each factory's eval code lives in `src/<product>/` there. One start folder then works for both teams;
  an agent whose checkout lacks the folder doesn't start (`factory-cd: product folder … does not exist`).
- One GitHub label per factory slug, as in step 5: `gh label create project1 -R "$GITHUB_REPO"`.

### M2. One env file per factory

Copy `config/factory.env` to `config/project1.env` and set:

```bash
BUILD_PROJECT=project1
EVAL_PROJECT=project1-eval
AGENT_PREFIX=project1                 # the full build slug
FACTORY_SUBDIR=src/project1           # also selects the <harness>-factory harness-configs
```

Keep `GITHUB_REPO`, `BUILD_BRANCH` and `EVAL_BRANCH` the same for every factory.

**Check:** `FACTORY_ENV=config/project1.env scripts/render-templates.py --src <kit>/templates --agent-names` prints `project1-lead` for the
`lead` role, and `FACTORY_ENV=config/project1.env scripts/render-templates.py --src <kit>/templates --out /tmp/p1` renders without
"missing values".

On a Hub that already runs other factories, run the read-only preflight now:
`FACTORY_ENV=config/project1.env scripts/run-on-hub.sh scripts/hub-preflight.py`. It warns when a slug is taken and
prints the merged `FACTORY_PROJECTS` to use in M7.

### M3. Harness-configs that start in the product folder (once per Hub, again after every Scion upgrade)

```bash
FACTORY_ENV=config/project1.env scripts/make-factory-harness-configs.py --via-hub            # plan
FACTORY_ENV=config/project1.env scripts/make-factory-harness-configs.py --via-hub --apply    # build and publish
```

It publishes `claude-factory`, `antigravity-factory`, `opencode-factory` and `hermes-factory` with the PO's admin
rights (`PO_EMAIL` must have signed in to the web UI once). The bundled configs stay untouched.

**Check:** on the VM, `scion harness-config list --hub` lists the `-factory` configs.

### M4. Hub projects (per factory)

Put a GitHub token on the VM for the Hub OS user, over SSH stdin so it never lands on a command line:

```bash
set -a; . config/project1.env; set +a
read -rs GITHUB_PAT; printf '%s' "$GITHUB_PAT" | gcloud compute ssh "$HUB_VM" --project "$HUB_PROJECT" --zone "$HUB_ZONE" \
  --command "sudo -u $HUB_OS_USER sh -c 'umask 077; cat > ~/.factory-github-token'"; unset GITHUB_PAT
```

Create both projects from the same git URL. `shared` (one checkout per Hub project) matches the commit-slot protocol
in `_shared/agents.md` and gives the watchdog the checkout it copies the knowledge plane from:

```bash
FACTORY_ENV=config/project1.env scripts/run-on-hub.sh scripts/scion-admin.py project-create --slug project1 \
  --git-url "https://github.com/$GITHUB_REPO.git" --branch main --mode shared --token-file '~/.factory-github-token'
FACTORY_ENV=config/project1.env scripts/run-on-hub.sh scripts/scion-admin.py project-create --slug project1-eval \
  --git-url "https://github.com/$GITHUB_REPO.git" --branch eval-dashboard --mode shared --token-file '~/.factory-github-token'
```

(Keep `'~/…'` quoted so the path is expanded on the VM, not on your machine.)

The Hub stores the token as each project's `GITHUB_TOKEN` secret with injection mode **always**, so agents can
push. The branch is sent as the `scion.dev/default-branch` label (the create request has no branch field).

(`--mode per-agent` gives each agent its own clone on `scion/<agent>`; the leads then point out that the
shared-checkout protocol doesn't match, and there is no host checkout to copy the knowledge plane from.)

**Check:** `scripts/run-on-hub.sh scripts/scion-admin.py projects` lists both slugs, and on the VM
`git -C ~/.scion/projects/project1 branch --show-current` prints `main` and
`git -C ~/.scion/projects/project1-eval branch --show-current` prints `eval-dashboard`.

### M5. Shared directories (per factory)

```bash
FACTORY_ENV=config/project1.env scripts/run-on-hub.sh scripts/scion-admin.py shared-dirs            # plan
FACTORY_ENV=config/project1.env scripts/run-on-hub.sh scripts/scion-admin.py shared-dirs --apply
```

New Hub projects already have `scratchpad`; the first run usually creates only `knowledge-plane`.

**Check:** a second run prints `ok (scratchpad, knowledge-plane)` for `project1` and `project1-eval`.

### M6. Templates (per factory)

```bash
export FACTORY_ENV=config/project1.env
scripts/push-templates.py --project project1 --templates <kit>/templates --roles lead,backend,frontend,reviewer,deployer --apply
scripts/push-templates.py --project project1-eval --templates <kit>/templates \
  --roles eval-lead,factory-analyst,search-evaluator,ui-craftsman,eval-deployer --apply
```

**Check:** each run ends with `other templates unchanged: yes`.

### M7. Watchdog and knowledge planes (once per Hub, covering every factory; before the agents start)

Install the watchdog and the status sync with every factory project on the Hub (on a shared Hub, the merged list
that the preflight prints, so other factories stay covered) and each monorepo factory's knowledge plane
(`<build slug>=<path in that project's checkout>`; the eval project follows its factory). Factories that aren't in
the map, such as compiled kits, keep syncing their own `knowledge-plane/`:

```bash
FACTORY_ENV=config/project1.env \
FACTORY_PROJECTS=project1,project1-eval,project2,project2-eval \
KNOWLEDGE_PLANE_SOURCES=project1=src/project1/knowledge-plane,project2=src/project2/knowledge-plane \
  scripts/run-on-hub.sh scripts/factory-watchdog.py --install-cron --apply
FACTORY_ENV=config/project1.env FACTORY_PROJECTS=project1,project1-eval,project2,project2-eval \
  scripts/install-sync-cron.sh
```

Values that you set on the command line, as here, win over the env file.

The install run copies the knowledge planes right away (also into projects with no agent yet) and writes both
settings into `~/.scion/watchdog.env`, so the cron runs every 5 minutes keep them.

**Check:** the output shows `Check 5 (Shared Knowledge Plane): project1-eval: synced from
…/projects/project1/src/project1/knowledge-plane` for every project, and `crontab -l` on the VM lists
`factory-watchdog.py`.

### M8. Agents and heartbeats (per factory)

`FACTORY_ENV=config/project1.env scripts/render-templates.py --src <kit>/templates --agent-names` prints the names. Start the two leads as
the admin on the VM (`--harness-auth vertex-ai` for Claude Code on Vertex AI); each lead starts its prefixed team
(`project1-backend`, …) at its first wake-up, as its children:

```bash
scion start project1-lead -t lead --project=project1 --harness-auth vertex-ai "Read your instructions, then run your wake-up steps."
scion start project1-eval-lead -t eval-lead --project=project1-eval --harness-auth vertex-ai "Read your instructions, then run your wake-up steps."
```

Heartbeats target the prefixed leads (step 9):
`scion-admin.py schedule create --project project1 --name project1-lead-heartbeat --agent project1-lead …`, and the
same for `project1-eval` / `project1-eval-lead`.

**Check:** `scion list --project=project1` shows `project1-lead` with harness-config `claude-factory`. Inside its
container (`project1--project1-lead`): `readlink /proc/<claude pid>/cwd` is `/workspace/src/project1`,
`ls /workspace/src` lists every product, `ls /scion-volumes` shows `knowledge-plane scratchpad`, and the terminal
shows no trust prompt. Inside `project1--project1-eval-lead`, `cat /scion-volumes/knowledge-plane/INDEX.md` shows
project1's index (project2's agents show project2's), and writing there fails (read-only).

### Notes

- Claude Code agents need the `scion-claude` image on the VM (`deploy.sh` with `container_images.source: build`
  builds only Antigravity: `cd /opt/scion-source && docker build -t scion-claude:latest --build-arg
  BASE_IMAGE=scion-base:latest -f harnesses/claude/Dockerfile harnesses/claude/` and tag it
  `localhost/scion/scion-claude:latest`) and the Claude model enabled in Model Garden for `HUB_PROJECT`.
- Agents message only within their Hub project: `project1-lead` can't reach `project1-eval-lead` (`agent_not_found`).
  Build and eval teams coordinate through GitHub issues, Firestore and the read-only knowledge plane, as in a single
  factory.
- The start folder is a default, not a sandbox. Enforce path boundaries with CODEOWNERS and CI path filters if you
  need them.
- Fresh Hubs store the default template as blobs; `push-templates.py` then uploads the role files only (the Hub still
  layers the default template underneath).
