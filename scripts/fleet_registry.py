"""Fleet registry operations."""
import hashlib

FLEET_COLLECTION = "factory_fleet"
SNAPSHOT_COLLECTION = "factory_fleet_snapshots"
REQUIRED_FIELDS = ("slug", "project_id", "sandbox_project", "repo", "subdir", "owners", "identities", "baseline", "state")

class FleetRegistry:
    def __init__(self, store):
        self.store = store

    def get(self, slug: str) -> dict | None:
        return self.store.get_document(FLEET_COLLECTION, slug)

    def put(self, record: dict) -> None:
        missing = [f for f in REQUIRED_FIELDS if f not in record]
        if missing:
            raise ValueError(f"Missing REQUIRED_FIELDS: {', '.join(missing)}")
        self.store.upsert_document(FLEET_COLLECTION, record["slug"], record)

    def active(self) -> list[dict]:
        docs = self.store.list_documents(FLEET_COLLECTION)
        return sorted([d for d in docs if d.get("state") == "active"], key=lambda d: d.get("slug", ""))

    def find_by_sandbox(self, sandbox: str) -> dict | None:
        docs = self.store.list_documents(FLEET_COLLECTION)
        for d in docs:
            if d.get("sandbox_project") == sandbox and d.get("state") != "retired":
                return d
        return None

    def record_hashes(self, slug: str, hashes: dict[str, str], checked_at: str) -> None:
        record = self.get(slug)
        if record:
            record["current_hashes"] = hashes
            record["checked_at"] = checked_at
            self.put(record)

    def mark_retired(self, slug: str) -> None:
        record = self.get(slug)
        if record:
            record["state"] = "retired"
            self.put(record)

    def platform_paused(self, slug: str) -> set[str]:
        record = self.get(slug)
        if record:
            return set(record.get("paused_names", []))
        return set()

    def set_platform_paused(self, slug: str, names: set[str]) -> None:
        record = self.get(slug)
        if record:
            record["paused_names"] = sorted(list(names))
            self.put(record)

    def get_snapshot(self, slug: str) -> dict[str, str]:
        doc = self.store.get_document(SNAPSHOT_COLLECTION, slug)
        return doc if doc else {}

    def put_snapshot(self, slug: str, contents: dict[str, str]) -> None:
        capped = {}
        for k, v in contents.items():
            if len(v) > 200_000:
                h = hashlib.sha256(v.encode('utf-8')).hexdigest()
                capped[k] = f"sha256:{h}"
            else:
                capped[k] = v
        self.store.upsert_document(SNAPSHOT_COLLECTION, slug, capped)
