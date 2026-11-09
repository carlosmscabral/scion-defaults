# Factory control plane on Firestore

The factory stores agent status and evaluation runs in Firestore (Native mode). Firestore is serverless: there is no
instance to size, and idle cost is zero. The factory never needs Cloud Spanner.

| Collection | Document ID | Writer | Reader |
|---|---|---|---|
| `factory_agent_status` | `<project_slug>__<agent_name>` | [`scripts/sync-factory-agents.py`](../../scripts/sync-factory-agents.py) on the Hub VM, every minute | Eval agents, dashboard |
| `eval_runs` | `<run_id>` | Eval agents, with [`scripts/factory_store.py`](../../scripts/factory_store.py) `put-eval-run` | Eval agents, dashboard |
| `factory_fleet` | `<slug>` | `scripts/fleet_registry.py` | Control plane |
| `factory_fleet_snapshots` | `<slug>` | `scripts/fleet_registry.py` | Control plane |

- [`firestore.indexes.json`](firestore.indexes.json): composite indexes. [`setup-sandbox.sh`](../../scripts/setup-sandbox.sh)
  creates them.
- [`firestore.rules`](firestore.rules): denies every client SDK request. All access is server side, through IAM:
  `roles/datastore.user` for writers (the Hub VM service account) and `roles/datastore.viewer` for the dashboard.
- Field-level contract: [`docs/plans/2026-10-20-firestore-control-plane.md`](../../docs/plans/2026-10-20-firestore-control-plane.md).
  Every compiled kit also contains a project-specific `config/firestore/README.md`.

Inspect the data from any machine with Google credentials:

```bash
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-agents --project-slug "$BUILD_PROJECT"
python3 scripts/factory_store.py --project "$FIRESTORE_PROJECT" list-eval-runs --limit 5
```
