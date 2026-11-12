---
name: clean-room-reproducibility
description: >-
  Use when auditing or hardening scion-defaults (or any GCP/GKE bootstrap CLI
  and template compiler) so external operators can reproduce deployments on a
  clean machine and fresh Google Cloud organization without hidden author state.
---

# Clean-Room GCP & GKE Bootstrap Reproducibility

Deepen verification instead of expanding product scope. A GKE software factory bootstrap is reproducible only when an external operator on a clean machine with an unseeded repository reaches a healthy Scion Hub and working agent pods without undocumented fixes.

## 1. Audit Raw Sources Against Live GCP Constraints
1. Parse `scripts/*.py` via Python `ast` and `scripts/*.sh` via `bash -n` / `shellcheck` to extract every external binary (`gcloud`, `gke-gcloud-auth-plugin`, `terraform >= 1.9`, `kubectl`, `gh`, `jq`, `openssl`, `uv`), environment variable, and GCP/GitHub API call.
2. Diff `skills/scion-factory-bootstrap/SKILL.md`, `README.md` Starting Prompts, and `GRILL_QUESTIONS` against `config/factory.schema.json`, `scripts/gke_lifecycle.py`, and `scripts/provision_factory.py` so an interviewed `factory.yaml` never drifts from tested GKE flags (`runtime_profile: remote`, single-project `heartbeat_mode: delta`, `SCION_REF` pinning).
3. Check every GCP cold-start hazard learned from raw failure logs:
   - Missing `default` VPC or `compute.googleapis.com` `SERVICE_DISABLED` propagation on fresh GCP projects.
   - GCP org policies restricting end-user OIDC tokens against Cloud Run Direct IAP (`USE_OPERATOR_SA=1` service-account impersonation).
   - Cloud Resource Manager `429 RATE_LIMIT_EXCEEDED` bursts and `409 ABORTED` IAM policy ETag conflicts during `setup-sandbox.sh` / `setup-gke-hub.sh`.
   - Vertex AI Model Garden enablement and `GOOGLE_CLOUD_LOCATION=global` project-scope Hub env verification before `scion start`.

## 2. Fail Fast With a `< 15s` Deterministic Preflight (`doctor`)
Before calling `gcloud projects create`, `terraform apply`, or a 40-minute Cloud Build, run a deterministic preflight (`DRY_RUN=1` and `--json` supported) that verifies:
- Local CLI presence and minimum versions (`terraform >= 1.9`, `kubectl`, `gke-gcloud-auth-plugin`, `gcloud`, `gh`, `uv`).
- GitHub token presence and required scopes (`repo`, plus `workflow` when issues touch `.github/workflows/*`) without printing secret bytes.
- GCP project/billing/folder access and actionable one-line remediation commands for every missing prerequisite.

## 3. Prove With Independent Clean-Room Oracles
1. **Hermetic `env -i` test suite**: Execute `inspect -> grill -> add-role -> check -> compile -> verify-factory.sh -> kp-check.py -> gke-up --dry-run` with isolated `HOME`, `TMPDIR`, and controlled `PATH` against unseeded repositories (Python, Go, monorepo `FACTORY_SUBDIR`, and `vcs.enabled: false`), without loading `presets/*.yaml`.
2. **Negative `doctor` injection tests**: Simulate missing `gke-gcloud-auth-plugin`, outdated `terraform`, and missing GitHub/GCP credentials, asserting failure in `< 10s` with exact fix instructions.
3. **Clean CI runner**: Run `pytest`, doc-contract checks, shell syntax checks, and hermetic bootstrap tests on a stock `ubuntu-latest` GitHub Actions runner on every push and pull request.
