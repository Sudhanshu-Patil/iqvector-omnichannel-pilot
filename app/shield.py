"""Pre-LLM BFSI shield: masks PII and flags prompt injection.

Pure regex, no I/O, well under a millisecond per message. Everything downstream (DB, logs, LLM)
only ever sees `ShieldResult.redacted_text`; the raw message never leaves `shield()`.
"""

import re
import unicodedata
from typing import NamedTuple

# Applied in order: IBAN and card numbers are long digit runs the phone patterns would otherwise
# swallow, and credentials/DOB/SSN must be caught before the generic digit rules. A pattern may
# capture a (?P<keep>...) label that stays readable, e.g. "CVV [REDACTED_CARD]".
_PII_PATTERNS = [
    ("[REDACTED_EMAIL]", re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}\b")),
    # "john dot doe at gmail dot com"
    ("[REDACTED_EMAIL]", re.compile(
        r"\b[\w+-]{1,64}(?:\s*(?:\.|\(dot\)|\[dot\]|\s+dot\s+)\s*[\w+-]{1,64}){0,3}"
        r"\s*(?:@|\(at\)|\[at\]|\s+at\s+)\s*[\w-]{1,63}(?:\s*(?:\(dot\)|\[dot\]|\s+dot\s+)\s*[\w-]{1,63}){1,3}\b",
        re.IGNORECASE,
    )),
    ("[REDACTED_ID]", re.compile(
        r"(?P<keep>\b(?:password|passwd|pwd|passcode|passphrase|pin|otp|one[-\s]time\s+(?:code|password|pin)"
        r"|verification\s+code|auth(?:entication)?\s+code)\b\s*(?:(?:is|was)\b|[:=])\s*)\S+",
        re.IGNORECASE,
    )),
    ("[REDACTED_ID]", re.compile(r"(?P<keep>\b(?:pin|otp)\b\W{0,3})\d{4,8}\b", re.IGNORECASE)),
    ("[REDACTED_ID]", re.compile(r"\b(?:sk|pk|rk|gsk|api[-_]?key)[-_][A-Za-z0-9_-]{16,}|\bAKIA[0-9A-Z]{16}\b")),
    ("[REDACTED_ID]", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")),  # IBAN
    # 13-19 digits with optional single separators. No Luhn check on purpose: over-masking is the
    # safe failure in BFSI. (?<!\+) keeps long international phone numbers out.
    ("[REDACTED_CARD]", re.compile(r"(?<![+\d])\b(?:\d[ .-]?){12,18}\d\b")),
    ("[REDACTED_CARD]", re.compile(r"(?P<keep>\b(?:CVV2?|CVC2?|CSC|security\s+code)\b\W{0,3})\d{3,4}\b", re.IGNORECASE)),
    ("[REDACTED_CARD]", re.compile(
        r"(?P<keep>\b(?:exp(?:iry|iration)?(?:\s+date)?|valid\s+(?:thru|through|until))\b\W{0,3})\d{1,2}\s*[/-]\s*\d{2,4}\b",
        re.IGNORECASE,
    )),
    ("[REDACTED_ID]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),  # US SSN
    ("[REDACTED_ID]", re.compile(
        r"(?P<keep>\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|born\s+on)\W{0,3})\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b",
        re.IGNORECASE,
    )),
    ("[REDACTED_ID]", re.compile(r"\b[A-Za-z]\d{7,8}[A-Za-z]?\b")),  # passport-style document number
    # International (+) and NANP formats. The lookbehind skips references like DHL-994817263.
    ("[REDACTED_PHONE]", re.compile(r"(?<![\w-])\+\d[\d ().-]{6,18}\d")),
    ("[REDACTED_PHONE]", re.compile(r"(?<![\w-])\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    # National format with a trunk 0, e.g. "07700 900123", "0641122334". Spaces only: dashes and
    # dots would turn dates like 01-12-2026 into phone numbers.
    ("[REDACTED_PHONE]", re.compile(r"(?<![\w+./-])0(?: ?\d){9,10}\b")),
    # Any other bare 9-12 digit run: account numbers, undashed SSNs, national IDs.
    ("[REDACTED_ID]", re.compile(r"(?<![\w+./-])\d{9,12}\b")),
]

_OVERRIDE_VERB = r"(?:ignore|forget|disregard|override|bypass|skip)"
_OVERRIDE_FILLER = (
    r"(?:\s+(?:all|any|the|your|these|those|previous|prior|above|earlier|system|safety"
    r"|security|knowledge\s+base|kb|grounded|grounding))*"
)
_OVERRIDE_TARGET = r"\s+(?:instructions?|rules?|prompts?|guardrails?|shield|safety|refusal|restrictions?|guidelines?)"

# Specific phrasings over broad keywords: a false positive blocks a real customer.
_INJECTION = re.compile(
    "|".join(
        [
            rf"\b{_OVERRIDE_VERB}{_OVERRIDE_FILLER}{_OVERRIDE_TARGET}\b",  # "ignore all previous instructions"
            # "disregard the above", but not "please ignore the above message"
            r"\b(?:ignore|disregard|forget)\s+(?:all\s+(?:of\s+)?)?(?:the\s+|everything\s+)?(?:above|preceding)\b"
            r"(?!\s+(?:message|email|mail|note|text|comment|request|question|order|address))",
            # The same override in other EU languages (de, es/it, fr, nl).
            r"\b(?:ignoriere\w*|ignora|ignorez|ignorer|vergiss\w*|oublie[rz]?|olvida\w*|dimentica\w*|negeer\w*)\b"
            r"[^.!?\n]{0,40}\b(?:instruc\w*|anweisung\w*|istruzion\w*|regeln|r[eè]gles|reglas|regole|prompt\w*)",
            r"\bsystem\s+(?:override|prompt)\b",
            r"\b(?:reveal|show|print|repeat|output|display|tell\s+me|what\s+(?:are|were|is))\b[^.!?\n]{0,25}"
            r"\b(?:your|the)\s+(?:initial\s+|original\s+|hidden\s+|system\s+|secret\s+|full\s+)?"
            r"(?:prompt|instructions|system\s+message)\b(?!\s+(?:for|on|to|about))",
            # Only a request to *switch into* a mode: "does the API have a developer mode?" is a fair question.
            r"\b(?:enter|enable|activate|switch\s+(?:to|into)|you\s+are\s+(?:now\s+)?in)\s+(?:\w+\s+)?"
            r"(?:developer|debug|admin|god|jailbreak)\s+mode\b",
            r"\b(?:pretend|roleplay|role-play)\b[^.!?\n]{0,15}\b(?:you\s+are|you're|to\s+be|as)\b",
            r"\bunredacted\b",
            r"\bselect\s+\*\s+from\b|\b(?:drop|truncate)\s+table\b|\bunion\s+select\b",
            r"\b(?:dump|list|print|export|output|show)\b[^.!?\n]{0,30}\ball\s+(?:rows|records|contacts|customers|users|emails)\b",
            r"\bdump\b[^.!?\n]{0,30}\b(?:contacts|tables?|database|db)\b",
            r"\[\s*(?:admin|system)|<\s*/?\s*system\s*>",  # fake privileged tags
            r"\b(?:invent|fabricate)\b[^.!?\n]{0,30}\b(?:fake|tracking|answers?|data|refunds?)\b",
        ]
    ),
    re.IGNORECASE,
)

_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁩﻿]")  # zero-width and bidi controls
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SPACED_LETTERS = re.compile(r"\b(?:\w[ .\-_*]){2,}\w\b")  # "i g n o r e" / "i.g.n.o.r.e"


def _replace(token: str):
    return lambda match: (match.groupdict().get("keep") or "") + token


def _detection_view(text: str) -> str:
    """Undo cheap obfuscation (leetspeak, spaced-out letters) for injection matching only."""
    text = text.lower().translate(_LEET)
    return _SPACED_LETTERS.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group()), text)


class ShieldResult(NamedTuple):
    redacted_text: str
    pii_entities: int
    injection: bool


def shield(text: str) -> ShieldResult:
    # NFKC folds full-width digits and look-alike letters, and zero-width / bidi control characters
    # are dropped, so none of them can slip a card number or an injection phrase past the patterns.
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))
    entities = 0
    for token, pattern in _PII_PATTERNS:
        text, found = pattern.subn(_replace(token), text)
        entities += found
    return ShieldResult(text, entities, bool(_INJECTION.search(_detection_view(text))))
