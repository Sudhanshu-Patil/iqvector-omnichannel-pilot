"""Hybrid retrieval over the FAQ knowledge base: BM25 + model2vec embeddings.

Orders are deliberately not indexed. They are reachable only by exact order ID, so a vague
question can never surface another customer's order.
"""

import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional

import numpy as np
from model2vec import StaticModel

PROJECT_DIR = Path(__file__).resolve().parent.parent
MODEL_ID = "minishlab/potion-base-8M"  # 30 MB static embeddings, CPU + numpy only
MODEL_DIR = Path(os.getenv("EMBEDDING_MODEL_DIR", PROJECT_DIR / "models" / "potion-base-8M"))

# Minimum cosine for an answer grounded by meaning alone (no curated keyword in the question).
# Calibrated in docs/DECISIONS.md: out-of-KB questions peak at 0.443, true paraphrases start at 0.478.
COSINE_GATE = 0.46

# Phrasings real customers use that the KB's own keyword lists miss. They extend the curated
# anchors; the KB file itself is left untouched.
EXTRA_ANCHORS = {
    "faq_04": ["burst", "burst capacity", "requests per second", "throttling", "api limit"],
    "faq_06": [
        "pay with", "pay by", "pay via", "pay using", "payment options", "paypal", "apple pay", "google pay",
        "amex", "american express", "bitcoin", "crypto", "cryptocurrency",
    ],
    "faq_07": [
        "opening hours", "office hours", "are you open", "when are you open", "open on weekends",
        "open on the weekend", "open at weekends", "open on saturday", "open on sunday", "weekend support",
    ],
}

_STOPWORDS = frozenset(
    "a an and any are as at be by can could do does for from had has have how i if in is it its me "
    "my of on or our please so that the their there this to us was we what when where which who "
    "will with would you your".split()
)


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        if token not in _STOPWORDS:
            # Crude plural folding ("limits" -> "limit"), applied identically to queries and docs.
            tokens.append(token[:-1] if len(token) > 3 and token.endswith("s") else token)
    return tokens


class BM25:
    """Okapi BM25 with the Lucene IDF. On a 7-document corpus the classic Okapi IDF goes negative
    for terms in more than half the documents; the Lucene form stays positive."""

    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.term_freqs = [Counter(doc) for doc in docs]
        self.lengths = [len(doc) for doc in docs]
        self.avg_length = sum(self.lengths) / len(docs)
        doc_freq = Counter(term for doc in docs for term in set(doc))
        self.idf = {term: math.log(1 + (len(docs) - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()}

    def scores(self, query: list[str]) -> list[float]:
        results = []
        for tf, length in zip(self.term_freqs, self.lengths):
            norm = self.k1 * (1 - self.b + self.b * length / self.avg_length)
            results.append(sum(self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in query if t in tf))
        return results


class Hit(NamedTuple):
    faq_id: str
    bm25: float
    cosine: float


@lru_cache(maxsize=1)
def load_model() -> StaticModel:
    return StaticModel.from_pretrained(str(MODEL_DIR) if MODEL_DIR.exists() else MODEL_ID)


def _unit(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)  # an emoji-only query embeds to all zeros


def _anchor_pattern(phrases: list[str]) -> re.Pattern:
    # Word boundaries so "sepa" never matches "separate"; an optional plural on the last word.
    alternatives = "|".join(r"\s+".join(map(re.escape, phrase.split())) for phrase in phrases)
    return re.compile(rf"\b(?:{alternatives})(?:s|es)?\b", re.IGNORECASE)


class KnowledgeBase:
    def __init__(self, faqs: list[dict], model: StaticModel):
        # Answers go to customers verbatim, so the KB's internal tool references are dropped
        # ("...a human operator must ... submit a carrier reroute request via escalate_to_human.").
        self.answers = {faq["id"]: re.sub(r"\s+via\s+escalate_to_human\b", "", faq["answer"]) for faq in faqs}
        self._ids = [faq["id"] for faq in faqs]
        # The KB ships curated keywords per FAQ; together with the topic name they are "anchors":
        # if one appears in the question, the FAQ is grounded deterministically.
        self._anchors = {
            faq["id"]: _anchor_pattern(
                [faq["topic"].replace("_", " "), *faq["keywords"], *EXTRA_ANCHORS.get(faq["id"], [])]
            )
            for faq in faqs
        }
        docs = [" ".join([faq["topic"].replace("_", " "), *faq["keywords"], faq["answer"]]) for faq in faqs]
        self._bm25 = BM25([tokenize(doc) for doc in docs])
        self._model = model
        self._vectors = _unit(model.encode(docs))

    def anchored(self, text: str) -> list[str]:
        return [faq_id for faq_id in self._ids if self._anchors[faq_id].search(text)]

    def search(self, text: str) -> list[Hit]:
        """All FAQs, best first, by reciprocal-rank fusion of BM25 and embedding cosine."""
        bm25 = self._bm25.scores(tokenize(text))
        cosine = self._vectors @ _unit(self._model.encode([text]))[0]
        # BM25 only ranks the FAQs it actually matched; a zero score carries no signal.
        by_bm25 = sorted((i for i in range(len(self._ids)) if bm25[i] > 0), key=lambda i: -bm25[i])
        by_cosine = sorted(range(len(self._ids)), key=lambda i: -cosine[i])
        fused = [0.0] * len(self._ids)
        for ranking in (by_bm25, by_cosine):
            for rank, i in enumerate(ranking):
                fused[i] += 1 / (60 + rank)
        best_first = sorted(range(len(self._ids)), key=lambda i: -fused[i])
        return [Hit(self._ids[i], bm25[i], float(cosine[i])) for i in best_first]

    def semantic_match(self, text: str) -> Optional[str]:
        """The FAQ a keyword-free question is clearly about, or None."""
        top = self.search(text)[0]
        # A meaning-only match needs lexical evidence too, so a vaguely similar question can't pass.
        return top.faq_id if top.bm25 > 0 and top.cosine >= COSINE_GATE else None
