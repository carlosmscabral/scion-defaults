import pytest
from fleet_registry import FleetRegistry
from pathlib import Path

ROOT = Path(__file__).parent.parent

def record(**over):
    base = {"slug": "shop", "project_id": "p-shop", "sandbox_project": "sandbox-a", "repo": "acme/shop",
            "subdir": "", "owners": [{"email": "ana@example.com", "github": "ana-gh"}],
            "identities": {"worker_ksa": "shop-worker"}, "baseline": {"commit": "abc", "hashes": {}},
            "state": "active"}
    return {**base, **over}

def test_put_requires_core_fields(memory_store):
    with pytest.raises(ValueError, match="sandbox_project"):
        FleetRegistry(memory_store).put({"slug": "shop"})

def test_active_excludes_retired_and_paused(memory_store):
    reg = FleetRegistry(memory_store)
    for slug, state in (("a1", "active"), ("b1", "retired"), ("c1", "paused")):
        reg.put(record(slug=slug, state=state, sandbox_project=f"sb-{slug}"))
    assert [r["slug"] for r in reg.active()] == ["a1"]

def test_find_by_sandbox_ignores_retired(memory_store):
    reg = FleetRegistry(memory_store)
    reg.put(record(slug="old", state="retired"))
    assert reg.find_by_sandbox("sandbox-a") is None

def test_snapshot_caps_large_values(memory_store):
    reg = FleetRegistry(memory_store)
    reg.put_snapshot("shop", {"template:lead/system-prompt.md": "x" * 200_001, "hook:h": "echo hi"})
    snap = reg.get_snapshot("shop")
    assert snap["template:lead/system-prompt.md"].startswith("sha256:") and snap["hook:h"] == "echo hi"

def test_firestore_store_document_methods_over_http(monkeypatch):
    import factory_store
    store = factory_store.FirestoreStore(project="host-proj", database="(default)", dry_run=False)
    
    requests_seen = []
    
    def fake_request(method, url, body=None):
        requests_seen.append((method, url, body))
        if method == "GET" and "factory_fleet/404" in url:
            raise RuntimeError("HTTP 404")
        if method == "GET" and "factory_fleet" in url and "pageSize" in url:
            if "pageToken=page2" in url:
                return {"documents": [{"name": "projects/host-proj/databases/(default)/documents/factory_fleet/d2", "fields": {"slug": {"stringValue": "d2"}}}]}
            return {"documents": [{"name": "projects/host-proj/databases/(default)/documents/factory_fleet/d1", "fields": {"slug": {"stringValue": "d1"}}}], "nextPageToken": "page2"}
        if method == "POST" and ":commit" in url:
            return {}
        return {}
    
    monkeypatch.setattr(store, "_request", fake_request)
    
    assert store.get_document("factory_fleet", "404") is None
    store.upsert_document("factory_fleet", "d3", {"slug": "d3"})
    
    docs = store.list_documents("factory_fleet")
    assert [d["slug"] for d in docs] == ["d1", "d2"]
    
    assert requests_seen[0][:2] == ("GET", "https://firestore.googleapis.com/v1/projects/host-proj/databases/(default)/documents/factory_fleet/404")
    assert requests_seen[1][0] == "POST"
    assert ":commit" in requests_seen[1][1]
    assert "update" in requests_seen[1][2]["writes"][0]
    assert "factory_fleet/d3" in requests_seen[1][2]["writes"][0]["update"]["name"]
    assert requests_seen[2][:2] == ("GET", "https://firestore.googleapis.com/v1/projects/host-proj/databases/(default)/documents/factory_fleet?pageSize=300")
    assert requests_seen[3][:2] == ("GET", "https://firestore.googleapis.com/v1/projects/host-proj/databases/(default)/documents/factory_fleet?pageSize=300&pageToken=page2")

def test_firestore_rules_deny_client_access_to_fleet_collections():
    rules = (ROOT / "config/firestore/firestore.rules").read_text()
    for coll in ("factory_fleet", "factory_fleet_snapshots"):
        assert f"match /{coll}/{{doc}}" in rules
    assert rules.count("allow read, write: if false") >= 2
