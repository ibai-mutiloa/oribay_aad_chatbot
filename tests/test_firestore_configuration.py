"""Firestore client configuration tests use inert in-memory clients only."""

import importlib
import os
from types import SimpleNamespace

import pytest


class _Snapshot:
    exists = False

    def to_dict(self):
        return {}


class _Document:
    def __init__(self, operations):
        self.operations = operations

    def get(self):
        self.operations.append(("get",))
        return _Snapshot()

    def set(self, payload):
        self.operations.append(("set", payload))

    def delete(self):
        self.operations.append(("delete",))


class _Collection:
    def __init__(self, name, operations):
        self.name = name
        self.operations = operations

    def document(self, document_id):
        self.operations.append(("document", self.name, document_id))
        return _Document(self.operations)


class _FakeFirestore:
    def __init__(self, operations):
        self.operations = operations

    def collection(self, name):
        self.operations.append(("collection", name))
        return _Collection(name, self.operations)


@pytest.fixture
def reload_app_with_cloud_clients_mocked(aad_app, monkeypatch):
    """Reload app.py after replacing every client constructor with a local fake."""
    original_database = os.environ.get("FIRESTORE_DATABASE")
    original_collection = os.environ.get("MEMORY_COLLECTION")
    constructor_calls = []
    memory_operations = []
    inert = SimpleNamespace()

    monkeypatch.setattr(aad_app.google.auth, "default", lambda **_kwargs: (inert, "test-project"))
    monkeypatch.setattr(aad_app.bigquery, "Client", lambda **_kwargs: inert)
    monkeypatch.setattr(aad_app.genai, "Client", lambda **_kwargs: inert)

    def fake_firestore_client(**kwargs):
        constructor_calls.append(kwargs)
        return _FakeFirestore(memory_operations)

    monkeypatch.setattr(aad_app.firestore, "Client", fake_firestore_client)

    def reload_with(database=None, collection=None):
        if database is None:
            monkeypatch.delenv("FIRESTORE_DATABASE", raising=False)
        else:
            monkeypatch.setenv("FIRESTORE_DATABASE", database)
        if collection is None:
            monkeypatch.delenv("MEMORY_COLLECTION", raising=False)
        else:
            monkeypatch.setenv("MEMORY_COLLECTION", collection)
        importlib.reload(aad_app)
        return aad_app, constructor_calls, memory_operations

    yield reload_with

    if original_database is None:
        os.environ.pop("FIRESTORE_DATABASE", None)
    else:
        os.environ["FIRESTORE_DATABASE"] = original_database
    if original_collection is None:
        os.environ.pop("MEMORY_COLLECTION", None)
    else:
        os.environ["MEMORY_COLLECTION"] = original_collection
    importlib.reload(aad_app)


def test_firestore_database_defaults_to_default_database(reload_app_with_cloud_clients_mocked):
    app_module, calls, _ = reload_app_with_cloud_clients_mocked()

    assert app_module.FIRESTORE_DATABASE == "(default)"
    assert calls[-1]["database"] == "(default)"


def test_firestore_database_and_memory_collection_are_independently_configurable(
    reload_app_with_cloud_clients_mocked,
):
    app_module, calls, operations = reload_app_with_cloud_clients_mocked(
        database="aad-eval-v83",
        collection="aad_eval_v83_memory",
    )

    assert app_module.FIRESTORE_DATABASE == "aad-eval-v83"
    assert calls[-1]["database"] == "aad-eval-v83"
    assert app_module.MEMORY_COLLECTION == "aad_eval_v83_memory"

    app_module.load_history("synthetic-conversation")
    app_module.save_turn("synthetic-conversation", [], "pregunta sintética", "respuesta sintética")
    assert app_module.clear_history("synthetic-conversation") is True

    used_collections = [entry[1] for entry in operations if entry[0] == "collection"]
    assert used_collections == ["aad_eval_v83_memory"] * 3
