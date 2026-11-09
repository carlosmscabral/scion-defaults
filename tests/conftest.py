import pytest

pytest_plugins = [
    "test_hub_client",
    "test_firestore_control_plane",
]

class MemoryStore:
    def __init__(self):
        self.db = {}

    def get_document(self, collection: str, doc_id: str) -> dict | None:
        return self.db.get(collection, {}).get(doc_id)

    def upsert_document(self, collection: str, doc_id: str, data: dict) -> None:
        if collection not in self.db:
            self.db[collection] = {}
        data_copy = dict(data)
        for k, v in data_copy.items():
            if v == "REQUEST_TIME":
                data_copy[k] = "2026-11-09T12:00:00Z"
        self.db[collection][doc_id] = data_copy

    def list_documents(self, collection: str) -> list[dict]:
        return list(self.db.get(collection, {}).values())

@pytest.fixture
def memory_store():
    return MemoryStore()