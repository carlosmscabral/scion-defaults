#!/usr/bin/env python3
"""Serverless factory control plane on Firestore: a standard-library REST client and CLI.

The Hub sync writes agent status, eval agents record evaluation runs, and the dashboard reads both. Everything runs
through the Firestore REST API with IAM credentials, so no client library or provisioned database is needed.
Data contract: docs/plans/2026-10-20-firestore-control-plane.md (and config/firestore/README.md in a compiled kit).

Configuration (environment or flags):
  FIRESTORE_PROJECT   project that holds the database (default: SANDBOX_PROJECT, then APP_GCP_PROJECT)
  FIRESTORE_DATABASE  database ID (default: (default))
  BILLING_PROJECT     quota project sent as x-goog-user-project (default: the Firestore project; L12)
  FIRESTORE_EMULATOR_HOST  host:port of a local Firestore emulator; when set, requests go there without credentials

Usage:
  factory_store.py list-agents [--project-slug SLUG]
  factory_store.py list-eval-runs [--project-slug SLUG] [--limit N]
  factory_store.py put-eval-run --file run.json        # or JSON on stdin
  factory_store.py --dry-run put-eval-run --file run.json   # print the request, write nothing
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AGENT_STATUS = "factory_agent_status"
EVAL_RUNS = "eval_runs"
CONTROL_STATE_COLLECTION = "factory_control_state"
MAX_WRITES_PER_COMMIT = 500
API = "https://firestore.googleapis.com/v1"

EVAL_RUN_REQUIRED = ("run_id", "project_slug", "overall_status", "primary_metric_name", "primary_metric_value")
_DOC_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------

def _rfc3339(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_value(value: Any) -> dict[str, Any]:
    """Encode a Python value as a Firestore REST `Value`."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, datetime.datetime):
        return {"timestampValue": _rfc3339(value)}
    if isinstance(value, dict):
        return {"mapValue": {"fields": to_fields(value)}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [to_value(v) for v in value]}}
    return {"stringValue": str(value)}


def to_fields(doc: dict[str, Any]) -> dict[str, Any]:
    return {str(k): to_value(v) for k, v in doc.items()}


def from_value(value: dict[str, Any]) -> Any:
    """Decode a Firestore REST `Value`. Timestamps stay RFC 3339 strings."""
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return value["booleanValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "timestampValue" in value:
        return value["timestampValue"]
    if "stringValue" in value:
        return value["stringValue"]
    if "mapValue" in value:
        return from_fields(value["mapValue"].get("fields", {}))
    if "arrayValue" in value:
        return [from_value(v) for v in value["arrayValue"].get("values", [])]
    return None


def from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: from_value(v) for k, v in fields.items()}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Token for the attached service account (metadata server), or the active gcloud account."""
    meta_timeout = 10 if os.environ.get("KUBERNETES_SERVICE_HOST") else 3
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=meta_timeout) as resp:
            token = json.loads(resp.read().decode()).get("access_token", "")
            if token:
                return token
    except (OSError, ValueError):
        pass
    try:
        res = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=15)
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


class FirestoreStore:
    """Minimal Firestore REST client. With dry_run=True, requests are printed as JSON lines, not sent."""

    def __init__(self, project: str, database: str = "(default)", billing_project: str | None = None,
                 dry_run: bool = False, timeout: int = 30):
        if not project:
            raise ValueError("Firestore project is not set (FIRESTORE_PROJECT or SANDBOX_PROJECT)")
        self.project = project
        self.database = database or "(default)"
        self.billing_project = billing_project or project
        self.dry_run = dry_run
        self.timeout = timeout
        self._token: str | None = None

    @property
    def api(self) -> str:
        emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
        return f"http://{emulator}/v1" if emulator else API

    @property
    def root(self) -> str:
        return f"projects/{self.project}/databases/{self.database}/documents"

    def doc_name(self, collection: str, doc_id: str) -> str:
        return f"{self.root}/{collection}/{doc_id}"

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        if self.dry_run:
            print(json.dumps({"method": method, "url": url, "body": body}, sort_keys=True))
            return [] if url.endswith(":runQuery") else {}
        if self._token is None:
            self._token = "owner" if os.environ.get("FIRESTORE_EMULATOR_HOST") else get_access_token()
            if not self._token:
                raise RuntimeError("no Google credentials: attach a service account or run gcloud auth login")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-goog-user-project": self.billing_project,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="ignore")[:500]
            raise RuntimeError(f"Firestore {method} {url} failed: HTTP {err.code}: {detail}") from err

    def commit(self, writes: list[dict[str, Any]]) -> int:
        """Apply writes in batches of at most 500. Returns the number of writes sent."""
        for start in range(0, len(writes), MAX_WRITES_PER_COMMIT):
            self._request("POST", f"{self.api}/{self.root}:commit",
                          {"writes": writes[start:start + MAX_WRITES_PER_COMMIT]})
        return len(writes)

    def run_query(self, structured_query: dict[str, Any]) -> list[dict[str, Any]]:
        """Return decoded documents (with `_name`) matching a structured query."""
        rows = self._request("POST", f"{self.api}/{self.root}:runQuery", {"structuredQuery": structured_query})
        docs = []
        for row in rows or []:
            doc = row.get("document")
            if doc:
                decoded = from_fields(doc.get("fields", {}))
                decoded["_name"] = doc["name"]
                docs.append(decoded)
        return docs

    def get_document(self, collection: str, doc_id: str) -> dict | None:
        """Fetch and decode a single document. Return None on 404."""
        try:
            doc = self._request("GET", f"{self.api}/{self.doc_name(collection, doc_id)}")
            return from_fields(doc.get("fields", {}))
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return None
            raise

    def upsert_document(self, collection: str, doc_id: str, data: dict) -> None:
        """Upsert a document, replacing entirely. 'REQUEST_TIME' values become server timestamps."""
        fields = {}
        transforms = []
        for k, v in data.items():
            if v == "REQUEST_TIME":
                transforms.append({"fieldPath": k, "setToServerValue": "REQUEST_TIME"})
            else:
                fields[k] = v
        writes = [{"update": {"name": self.doc_name(collection, doc_id), "fields": to_fields(fields)}}]
        if transforms:
            writes[0]["updateTransforms"] = transforms
        self.commit(writes)

    def list_documents(self, collection: str) -> list[dict]:
        """List all documents in a collection, following pagination."""
        docs = []
        page_token = None
        while True:
            url = f"{self.api}/{self.root}/{collection}?pageSize=300"
            if page_token:
                url += f"&pageToken={page_token}"
            resp = self._request("GET", url)
            for doc in resp.get("documents", []):
                docs.append(from_fields(doc.get("fields", {})))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return docs


def store_from_env(dry_run: bool = False, project: str | None = None, database: str | None = None) -> FirestoreStore:
    project = project or os.environ.get("FIRESTORE_PROJECT") or os.environ.get("SANDBOX_PROJECT") \
        or os.environ.get("APP_GCP_PROJECT", "")
    return FirestoreStore(
        project=project,
        database=database or os.environ.get("FIRESTORE_DATABASE", "(default)"),
        billing_project=os.environ.get("BILLING_PROJECT") or project,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Agent status (written by the Hub sync)
# ---------------------------------------------------------------------------

def agent_doc_id(project_slug: str, agent_name: str) -> str:
    return f"{project_slug}__{agent_name}"


def agent_status_writes(store: FirestoreStore, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert one document per agent; `synced_at` is set to the server's commit time."""
    return [{
        "update": {"name": store.doc_name(AGENT_STATUS, agent_doc_id(r["project_slug"], r["agent_name"])),
                   "fields": to_fields(r)},
        "updateTransforms": [{"fieldPath": "synced_at", "setToServerValue": "REQUEST_TIME"}],
    } for r in rows]


def prune_agent_status(store: FirestoreStore, older_than: datetime.datetime) -> int:
    """Delete agent status documents not refreshed since `older_than` (deleted agents)."""
    stale = store.run_query({
        "from": [{"collectionId": AGENT_STATUS}],
        "where": {"fieldFilter": {"field": {"fieldPath": "synced_at"}, "op": "LESS_THAN",
                                  "value": to_value(older_than)}},
    })
    return store.commit([{"delete": d["_name"]} for d in stale]) if stale else 0


def list_agents(store: FirestoreStore, project_slug: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"from": [{"collectionId": AGENT_STATUS}],
                             "orderBy": [{"field": {"fieldPath": "agent_name"}, "direction": "ASCENDING"}]}
    if project_slug:
        query["where"] = {"fieldFilter": {"field": {"fieldPath": "project_slug"}, "op": "EQUAL",
                                          "value": to_value(project_slug)}}
    return store.run_query(query)


# ---------------------------------------------------------------------------
# Evaluation runs (written by eval agents)
# ---------------------------------------------------------------------------

def validate_eval_run(run: dict[str, Any]) -> list[str]:
    problems = [f"missing required field {k}" for k in EVAL_RUN_REQUIRED if run.get(k) in (None, "")]
    if run.get("run_id") and not _DOC_ID.fullmatch(str(run["run_id"])):
        problems.append("run_id may only contain letters, digits, '.', '_' and '-'")
    value = run.get("primary_metric_value")
    if value is not None and not isinstance(value, (int, float)):
        problems.append("primary_metric_value must be a number")
    for idx, req in enumerate(run.get("requirements") or []):
        if not isinstance(req, dict) or "id" not in req or "status" not in req:
            problems.append(f"requirements[{idx}] must be an object with id and status")
    return problems


def put_eval_run(store: FirestoreStore, run: dict[str, Any]) -> None:
    problems = validate_eval_run(run)
    if problems:
        raise ValueError("; ".join(problems))
    fields = {k: v for k, v in run.items() if k != "evaluated_at"}
    store.commit([{
        "update": {"name": store.doc_name(EVAL_RUNS, str(run["run_id"])), "fields": to_fields(fields)},
        "updateTransforms": [{"fieldPath": "evaluated_at", "setToServerValue": "REQUEST_TIME"}],
    }])


def list_eval_runs(store: FirestoreStore, project_slug: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"from": [{"collectionId": EVAL_RUNS}],
                             "orderBy": [{"field": {"fieldPath": "evaluated_at"}, "direction": "DESCENDING"}],
                             "limit": limit}
    if project_slug:
        query["where"] = {"fieldFilter": {"field": {"fieldPath": "project_slug"}, "op": "EQUAL",
                                          "value": to_value(project_slug)}}
    return store.run_query(query)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Factory control plane on Firestore")
    parser.add_argument("--project", help="Firestore project (default: FIRESTORE_PROJECT or SANDBOX_PROJECT)")
    parser.add_argument("--database", help="Firestore database ID (default: FIRESTORE_DATABASE or (default))")
    parser.add_argument("--dry-run", action="store_true", help="print requests instead of sending them")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_agents = sub.add_parser("list-agents", help="list synced agent status documents")
    p_agents.add_argument("--project-slug")
    p_runs = sub.add_parser("list-eval-runs", help="list recent evaluation runs")
    p_runs.add_argument("--project-slug")
    p_runs.add_argument("--limit", type=int, default=20)
    p_put = sub.add_parser("put-eval-run", help="record one evaluation run (JSON object)")
    p_put.add_argument("--file", help="path to a JSON file (default: stdin)")
    args = parser.parse_args(argv)

    try:
        store = store_from_env(dry_run=args.dry_run, project=args.project, database=args.database)
        if args.cmd == "list-agents":
            docs = list_agents(store, args.project_slug)
        elif args.cmd == "list-eval-runs":
            docs = list_eval_runs(store, args.project_slug, args.limit)
        else:
            raw = open(args.file).read() if args.file else sys.stdin.read()
            put_eval_run(store, json.loads(raw))
            if not args.dry_run:
                print("recorded", file=sys.stderr)
            return 0
        if not args.dry_run:
            print(json.dumps(docs, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# Control State (written by watchdog)
# ---------------------------------------------------------------------------

def load_control_state(store, state_id: str) -> dict:
    default_state = {"restarts": {}, "alerted": [], "last_user_msg_ts": ""}
    # store could be a duck-typed fake_firestore that implements get_document
    if hasattr(store, "get_document"):
        doc = store.get_document(CONTROL_STATE_COLLECTION, state_id)
    else:
        # Our FirestoreStore _request can do GET
        try:
            doc = store._request("GET", f"{store.api}/{store.doc_name(CONTROL_STATE_COLLECTION, state_id)}")
            if "fields" in doc:
                doc = from_fields(doc["fields"])
            else:
                doc = None
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                doc = None
            else:
                raise
    
    if doc and "state_json" in doc:
        try:
            loaded = json.loads(doc["state_json"])
            default_state.update(loaded)
        except json.JSONDecodeError:
            pass
    return default_state

def save_control_state(store, state_id: str, state: dict) -> None:
    state_json = json.dumps(state)
    fields = {
        "state_json": state_json,
    }
    
    if hasattr(store, "upsert_document"):
        store.upsert_document(CONTROL_STATE_COLLECTION, state_id, {"state_json": state_json, "updated_at": "REQUEST_TIME"})
    else:
        writes = [{
            "update": {"name": store.doc_name(CONTROL_STATE_COLLECTION, state_id), "fields": to_fields(fields)},
            "updateTransforms": [{"fieldPath": "updated_at", "setToServerValue": "REQUEST_TIME"}],
        }]
        store.commit(writes)
