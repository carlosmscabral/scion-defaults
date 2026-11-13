# Agent templates

`scion-defaults` generates role templates deterministically from a `FactorySpec` (`factory.yaml`) rather than shipping hand-edited static role folders in this directory.

[`scripts/role_library.py`](../scripts/role_library.py) and [`scripts/scion_factory.py`](../scripts/scion_factory.py) are the single source of truth for compiled role protocols, specialist definitions (`researcher`, `doc-writer`, `investigator`, `kaizen-analyst`, `ui-craftsman`, and catalog roles), harness launchers, repository focus paths, and human-on-the-loop decision rights.

Compile a complete kit (`<kit_dir>/templates/`) from your `factory.yaml` or any reference preset in [`presets/`](../presets/):

```bash
./scripts/scion-factory compile presets/starter-service.factory.yaml --out /tmp/kit
# Or compile the 10-role hybrid search preset:
./scripts/scion-factory compile presets/psearch.factory.yaml --out /tmp/psearch-kit
```

Render environment placeholders (`${SANDBOX_PROJECT}`, `${FIRESTORE_PROJECT}`, `${SERVING_SERVICE}`, etc.) against your `config/factory.env`:

```bash
scripts/render-templates.py --src /tmp/kit/templates --out build/templates
```

## Files in a compiled role folder

Each compiled kit writes `<kit_dir>/templates/_shared/agents.md` plus one subfolder per role in the build and evaluation rosters:

| File | Scion field | Notes |
|---|---|---|
| `scion-agent.yaml` | template config | `default_harness_config`, `system_prompt`, `agent_instructions`, and `env` (`FACTORY_ROLE`, `FACTORY_SUBDIR` on monorepo kits, `GOOGLE_CLOUD_LOCATION` on Vertex Gemini roles, and `CLOUDSDK_CORE_DISABLE_PROMPTS` on deployers) |
| `system-prompt.md` | `system_prompt` | Role responsibility, specialist protocol from `role_library.py`, repository focus paths, chain of command, scratchpad brief rules, L1-L5 evaluation oracle, and operating loop |
| `agents.md` | `agent_instructions` | Shared coordination protocol (`_shared/agents.md`, plus `agents.extra.md` when present) |
| `home/.local/bin/opencode-launch.sh` and `opencode` | home overlay (OpenCode roles) | Pinned Vertex AI model catalog (`~/.config/opencode/opencode.json`), auto-approve tool permissions, `GH_TOKEN` / `GITHUB_TOKEN` wiring, and `sciontool` PATH fix (L3, L26) |
| `home/.local/bin/hermes-launch.sh` and `hermes` | home overlay (Hermes roles) | Pinned `VERTEX_REGION=global` (`~/.hermes/.env`) and `hermes chat --yolo --accept-hooks --provider vertex -m <model>` wrapper (L4, L26) |

Harnesses without a native system-prompt flag (OpenCode, Hermes) automatically prepend `system_prompt` to the instructions file.

### Names and harness-configs

Compiled templates support single-repository factories and multi-factory monorepos (`monorepo.subdir` and `monorepo.agent_prefix` in `factory.yaml`, or `FACTORY_SUBDIR` and `AGENT_PREFIX` in `factory.env`):

| Setting | Compiled / rendered value | Purpose |
|---|---|---|
| Agent names (`_agent(spec, role)` / `${<ROLE>_AGENT}`) | `<agent_prefix>-<role>` (`project1-lead`), or bare `<role>` | Isolates multiple factories sharing one Scion Hub |
| `default_harness_config` | `<harness>-factory` when `monorepo.subdir` or Kubernetes runtime requires [`factory-cd.sh`](../config/harness/home/.local/bin/factory-cd.sh), else `<harness>` | Starts agents inside `/workspace/<subdir>` and refreshes GKE Direct IAP tokens |
| Knowledge plane path | `/scion-volumes/knowledge-plane/INDEX.md` (or `/workspace/<subdir>/knowledge-plane/INDEX.md` on shared-workspace Docker builds) | Gives every build and evaluation role the same requirements, ADRs, and `state/LIVE.md` |

List resolved Hub agent names before manual `scion start` calls:

```bash
scripts/render-templates.py --src /tmp/kit/templates --agent-names
```

See [docs/plans/2026-10-22-monorepo-factories.md](../docs/plans/2026-10-22-monorepo-factories.md) for monorepo design details.

## Uploading to the Hub

 For automated end-to-end setup, `./scripts/scion-factory provision <factory.yaml> --kit <kit_dir> --env <env_file> --apply` renders and uploads all build and evaluation templates over HTTPS or SSH.

To push templates directly, pass `--templates <kit_dir>/templates` to [`scripts/push-templates.py`](../scripts/push-templates.py) (add `--direct` when `HUB_URL` is set, and omit `--apply` for a dry run):

```bash
scripts/push-templates.py --project "$BUILD_PROJECT" --templates /tmp/kit/templates \
  --roles lead,backend,frontend,reviewer,deployer
scripts/push-templates.py --project "$BUILD_PROJECT" --templates /tmp/kit/templates \
  --roles lead,backend,frontend,reviewer,deployer --apply
```

Each template owns an isolated storage path on the Hub. If a legacy database row shares another template's storage path (L16), `push-templates.py` renames the colliding row to `<role>-legacy` and creates a clean template rather than overwriting shared files. Never edit `hub.db` directly.

Template changes take effect when an agent is created or reincarnated. For live incremental updates (adding a specialist role or broadcasting a rule delta to running agents), use `./scripts/scion-factory hot-upgrade` as documented in [operations-runbook.md](../docs/operations-runbook.md#automated-live-hot-upgrade-scion-factory-hot-upgrade).
