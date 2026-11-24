import pytest

from app.rag import BM25, tokenize
from tests.cases import CASES

OUT_OF_KB_RETRIEVAL_TRAPS = ["C11", "C13", "C14", "C15", "C16", "C17", "C18", "C19", "C20"]  # C12 is an order ID


def test_bm25_idf_stays_positive_for_terms_in_most_documents():
    bm25 = BM25([["order", "refund"], ["order"], ["order", "sepa"]])
    assert min(bm25.idf.values()) > 0
    scores = bm25.scores(tokenize("SEPA orders"))
    assert scores[2] == max(scores)


def test_tokenize_drops_stopwords_and_folds_plurals():
    assert tokenize("What are your rate limits?") == ["rate", "limit"]


def test_anchors_match_whole_words_and_plurals(kb):
    assert kb.anchored("Do you accept SEPA transfers?") == ["faq_06"]
    assert kb.anchored("Please keep these separate") == []
    assert kb.anchored("What payment methods do you take?") == ["faq_06"]
    assert kb.anchored("what are your SUPPORT OPERATING HOURS") == ["faq_07"]


@pytest.mark.parametrize("case_id", OUT_OF_KB_RETRIEVAL_TRAPS)
def test_out_of_kb_questions_are_not_grounded(kb, case_id):
    text = CASES[case_id]["text"]
    assert kb.anchored(text) == []
    assert kb.semantic_match(text) is None


@pytest.mark.parametrize(
    ("text", "faq_id"),
    [
        ("Which payment options can I use to pay you?", "faq_06"),
        ("Can I cancel my subscription and get refunded?", "faq_03"),
        ("Is my data kept inside the EU?", "faq_05"),
    ],
)
def test_paraphrases_are_grounded_by_meaning(kb, text, faq_id):
    assert kb.semantic_match(text) == faq_id


def test_customer_facing_answers_hide_internal_tool_names(kb):
    assert all("escalate_to_human" not in answer for answer in kb.answers.values())
    assert kb.answers["faq_01"].endswith("must manually submit a carrier reroute request.")


def test_order_questions_without_an_id_are_not_grounded(kb):
    assert kb.semantic_match("What is the status of my order?") is None
