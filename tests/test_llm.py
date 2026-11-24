import httpx
import pytest

from app import llm

CONTEXT = {
    "faq_03": "Customers can request a full refund within 14 calendar days of delivery for physical "
    "hardware keys and within 30 calendar days of purchase for annual software subscriptions."
}


def groq_reply(content, prompt_tokens=300, completion_tokens=40):
    def fake_chat(payload):
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }

    return fake_chat


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")


def test_grounded_answer_is_used_and_costed(monkeypatch):
    monkeypatch.setattr(llm, "_chat", groq_reply('{"answer": "You have 14 days for hardware keys.", "grounded": true}'))
    result = llm.phrase("L1", "What is the refund window?", CONTEXT)
    assert result.answer == "You have 14 days for hardware keys."
    assert result.cost_usd == pytest.approx((300 * 0.075 + 40 * 0.30) / 1_000_000)
    assert result.tokens == 340


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"grounded": true}',
        '{"answer": "", "grounded": true}',
        '{"answer": "You have 14 days.", "grounded": false}',
        '{"answer": "You have 21 days to return it.", "grounded": true}',  # a number not in the context
    ],
)
def test_unusable_answers_fall_back_to_the_source(monkeypatch, content):
    monkeypatch.setattr(llm, "_chat", groq_reply(content))
    assert llm.phrase("L1", "What is the refund window?", CONTEXT) is None


def test_provider_errors_fall_back_to_the_source(monkeypatch):
    def unavailable(payload):
        raise httpx.ConnectError("groq unreachable")

    monkeypatch.setattr(llm, "_chat", unavailable)
    assert llm.phrase("L2", "What is the refund window?", CONTEXT) is None


def test_call_is_skipped_when_over_the_per_turn_budget(monkeypatch):
    monkeypatch.setattr(llm, "OUTCOME_VALUE_USD", 0.0001)
    monkeypatch.setattr(llm, "_chat", lambda payload: pytest.fail("LLM called over budget"))
    assert llm.phrase("L2", "What is the refund window?", CONTEXT) is None


def test_largest_allowed_prompt_fits_the_budget():
    # 4,000-char message cap + system prompt + a few KB chunks, on the most expensive tier.
    assert llm.estimated_max_cost(llm.MODELS["L2"], 8000) < llm.OUTCOME_VALUE_USD * llm.MAX_COST_SHARE
