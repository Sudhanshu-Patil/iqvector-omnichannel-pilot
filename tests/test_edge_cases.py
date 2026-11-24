"""Production-style messages (eval/production_edge_cases.json), checked in the no-LLM mode."""

import json
from pathlib import Path

import pytest

EDGE_CASES = json.loads(
    (Path(__file__).resolve().parent.parent / "eval" / "production_edge_cases.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", EDGE_CASES, ids=lambda c: f"{c['id']}-{c['category']}")
def test_edge_case(case, send, client):
    result = send(case["text"], channel=case.get("channel", "web"))
    expect = case["expect"]

    if "level" in expect:
        assert result["level_used"] in expect["level"]
    if "source" in expect:
        assert result["source_chunk_id"] == expect["source"]
    if "escalate" in expect:
        assert result["escalate"] is expect["escalate"]
    if "injection" in expect:
        assert result["prompt_injection_blocked"] is expect["injection"]
    for token in expect.get("masked", []):
        assert token in result["pii_redacted_text"]

    transcript = client.get(f"/api/v1/conversations/{result['conversation_id']}").json()
    stored = " ".join(m["text"] for m in transcript["messages"])
    for raw in expect.get("leaked", []):
        assert raw not in result["pii_redacted_text"]
        assert raw not in result["reply"]
        assert raw not in stored
