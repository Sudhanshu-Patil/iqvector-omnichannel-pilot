"""The 30 eval cases as regression tests, in the no-LLM (verbatim) mode."""

import pytest

from tests.cases import EVAL_CASES


@pytest.mark.parametrize("case", EVAL_CASES, ids=lambda c: c["case_id"])
def test_eval_case(case, send):
    # A fresh user per case, so an escalation in one case never locks another.
    result = send(case["text"], channel=case["channel"], user=f"user_{case['case_id']}", message_id=case["message_id"])

    if "expected_level" in case:
        assert result["level_used"] in case["expected_level"]
    if "expected_source" in case:
        assert result["source_chunk_id"] == case["expected_source"]
    if "expected_escalate" in case:
        assert result["escalate"] is case["expected_escalate"]
        assert result["conversation_state"] == ("waiting_human" if case["expected_escalate"] else "ai_active")
    if case.get("expected_injection_blocked"):
        assert result["prompt_injection_blocked"] is True
        assert result["cost_usd"] == case["expected_cost_usd"]
    for token in case.get("must_mask_tokens", []):
        assert token in result["pii_redacted_text"]
    assert result["duplicate_webhook_ignored"] is False


def test_l0_share_meets_target(send):
    levels = [
        send(c["text"], channel=c["channel"], user=f"user_{c['case_id']}")["level_used"] for c in EVAL_CASES
    ]
    assert levels.count("L0") / len(levels) >= 0.40


@pytest.mark.parametrize(
    ("text", "level", "source", "escalate"),
    [
        ("Hello!", "L0", None, False),
        ("Where is my DHL order ORD-7741?", "L0", "ORD-7741", False),
        ("Can I change the delivery address for ORD-8812?", "L0", "ORD-8812,faq_01", True),
        ("Can I still get a refund for ORD-9105?", "L2", "ORD-9105,faq_03", False),
        ("What is your refund window and do you accept SEPA?", "L2", "faq_03,faq_06", False),
        ("Which payment options can I use to pay you?", "L1", "faq_06", False),
        ("I want to speak to a real person", "L0", None, True),
        ("What is the status of my order?", "L2", None, True),
    ],
)
def test_other_routes(send, text, level, source, escalate):
    result = send(text)  # each test gets a fresh database, so the default user is fine
    assert (result["level_used"], result["source_chunk_id"], result["escalate"]) == (level, source, escalate)
