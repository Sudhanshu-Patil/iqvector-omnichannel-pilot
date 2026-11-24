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


PAYMENTS = {
    "faq_06": "We accept SEPA Instant transfers in EUR, international SWIFT wire transfers in USD/GBP/EUR, and "
    "corporate Visa/Mastercard cards. We do not accept cryptocurrency or cash payments."
}
STORAGE = {
    "faq_05": "All customer and candidate data is stored in our EU Sovereign Vault (Frankfurt / Dublin) encrypted with "
    "AES-256 at rest and TLS 1.3 in transit. Raw PII is redacted prior to any external LLM call."
}


@pytest.mark.parametrize(
    ("context", "question", "answer"),
    [
        # Each of these came back from gpt-oss with grounded=true during live testing.
        (PAYMENTS, "do u take paypal", "We do not accept PayPal."),
        (STORAGE, "GDPR?", "Data is stored in Frankfurt and Dublin, which aligns with GDPR requirements."),
        (PAYMENTS, "IBAN [REDACTED_ID] for the SEPA refund", "We can process a SEPA refund in EUR to the IBAN you provided."),
        ({"faq_04": "Standard Enterprise Node Licenses include a rate limit of 600 requests per minute."},
         "What's the rate limit for the Starter plan?", "The Starter plan includes 600 requests per minute."),
        (STORAGE, "Are you certified?", "Yes, our storage is fully certified and compliant."),
        # "us" occurs inside "customer": names must match whole words in the source.
        (STORAGE, "Is my data stored in the US?", "Your data is stored in Frankfurt and Dublin, not in the US."),
    ],
)
def test_live_hallucination_patterns_are_rejected(monkeypatch, context, question, answer):
    monkeypatch.setattr(llm, "_chat", groq_reply(f'{{"answer": "{answer}", "grounded": true}}'))
    assert llm.phrase("L1", question, context) is None


@pytest.mark.parametrize(
    ("context", "question", "answer"),
    [
        (PAYMENTS, "Can I pay with Bitcoin?", "No, we do not accept cryptocurrency payments."),
        (PAYMENTS, "Which payment options can I use?",
         "You can pay by SEPA Instant transfer in EUR, SWIFT wire in USD, GBP or EUR, or corporate Visa or Mastercard cards."),
        (STORAGE, "Where is my data stored?", "Your data is stored in our EU Sovereign Vault in Frankfurt and Dublin, encrypted with AES-256."),
    ],
)
def test_faithful_paraphrases_pass_the_checks(monkeypatch, context, question, answer):
    monkeypatch.setattr(llm, "_chat", groq_reply(f'{{"answer": "{answer}", "grounded": true}}'))
    assert llm.phrase("L1", question, context).answer == answer


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
