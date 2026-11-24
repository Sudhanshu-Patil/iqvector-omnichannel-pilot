import json
from itertools import count

import pytest
from fastapi.testclient import TestClient

from app.main import KB_FILE, create_app
from app.rag import KnowledgeBase, load_model


@pytest.fixture(scope="session")
def kb():
    return KnowledgeBase(json.loads(KB_FILE.read_text(encoding="utf-8"))["faq_kb"], load_model())


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # tests never reach the network
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


@pytest.fixture
def send(client):
    """POST a CONTRACT #1 message; unique message ids unless one is given."""
    ids = count(1)

    def _send(text, *, channel="web", user="visitor_1", message_id=None, expect=200, **hints):
        payload = {
            "channel": channel,
            "external_user_id": user,
            "message_id": message_id or f"test_msg_{next(ids)}",
            "text": text,
            "identity_hints": hints,
            "ts": "2026-11-24T10:00:00Z",
        }
        response = client.post("/api/v1/message", json=payload)
        assert response.status_code == expect, response.text
        return response.json()

    return _send
