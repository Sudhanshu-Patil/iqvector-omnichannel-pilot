"""Pre-LLM BFSI shield: masks PII and flags prompt injection.

Pure regex, no I/O, well under a millisecond per message. Everything downstream (DB, logs, LLM)
only ever sees `ShieldResult.redacted_text`; the raw message never leaves `shield()`.

Masks into [REDACTED_EMAIL], [REDACTED_PHONE], [REDACTED_CARD] and [REDACTED_ID] as the brief
requires, plus [REDACTED_ADDRESS] and [REDACTED_NAME], which go beyond it: names are personal data
under GDPR, and the KB promises that raw PII never reaches an external LLM.
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

# Street addresses and postcodes. A street line needs a house number plus a street-type word,
# and the words in between may not be filler, so "2 questions about your road map" survives.
_STREET_FILLER = r"(?i:about|your|my|the|a|an|of|for|to|in|on|and|or|with|per|by|days?|weeks?|times?|requests?|orders?|items?)\b"
_STREET_TYPE = r"(?i:street|st|road|rd|avenue|ave|lane|ln|boulevard|blvd|drive|dr|court|ct|way|place|pl|square|sq|terrace|close|crescent)"
_ADDRESS_PATTERNS = [
    re.compile(rf"\b\d{{1,5}}[A-Za-z]?,?\s+(?:(?!{_STREET_FILLER})[A-Za-z][\w'-]*\s+){{1,3}}{_STREET_TYPE}\b\.?"),
    re.compile(r"\bVia\s+[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,2}\s+\d{1,4}[A-Za-z]?\b"),  # Italian, "Via Roma 10"
    re.compile(r"\b(?i:viale|piazza|corso|rue|calle|carrer|avenida|ulica|bulevar)\s+(?:[\w'-]+\s+){1,3}\d{1,4}[A-Za-z]?\b"),
    re.compile(r"\b[\w'-]*(?i:straße|strasse|str\.|weg|gasse|platz|allee)\s+\d{1,4}[a-z]?\b"),  # German, "Hauptstraße 5"
    re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"),  # UK postcode, "NW1 6XE"
    re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"),  # US state + ZIP, "CA 94105"
]

# Person names, where support chat actually contains them: a customer introducing themselves or
# signing off, or a title before a surname. Names of known customers are added at startup
# (set_known_names). A general NER model was measured and rejected: see docs/DECISIONS.md.
# Elena, O'Brien, McDonald, Jean-Luc; a title is never part of the name itself.
_NAME_WORD = r"(?!(?:Mr|Mrs|Ms|Miss|Dr|Prof)\b)(?:[A-Z]')?[A-Z][a-z]+(?:[A-Z][a-z]+)?(?:-[A-Z][a-z]+)*"
_NAME = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}"
# What follows a self-introduction: "I'm Elena Rostova, ...", "I am David Chen from Acme", "I'm Elena."
# Without it, "I'm Ready to upgrade" would lose "Ready".
_AFTER_INTRO = r"(?=\s*(?:[,.!?;:)]|$|\b(?:from|and|here|with|at|of)\b))"
_NOT_A_NAME = r"(?i:on|in|at|the|a|an|not|is|was|and|or|but|for|with|from|to|of|my|your|still|also|correct|wrong|spelled|missing|listed)\b"
_ANY_CASE_NAME = rf"(?!{_NOT_A_NAME})[A-Za-z][a-z'-]+(?:\s+(?!{_NOT_A_NAME})[A-Za-z][a-z'-]+){{0,2}}"
_NAME_PATTERNS = [
    # "my name is marco rossi": the cue is unambiguous, so lowercase names count too.
    re.compile(rf"(?P<keep>\b(?i:my\s+name\s+is|my\s+name's|name\s*:)\s+){_ANY_CASE_NAME}"),
    re.compile(rf"(?P<keep>\b(?i:i\s+am|i'm)\s+){_NAME}{_AFTER_INTRO}"),
    re.compile(rf"(?P<keep>(?:^|\n)[ \t]*(?i:(?:hi|hello|hey)[,!]?\s+)?(?i:this\s+is)\s+){_NAME}{_AFTER_INTRO}"),
    re.compile(rf"(?P<keep>\b(?i:mr|mrs|ms|miss|dr|prof)\.?\s+){_NAME}"),
    re.compile(
        rf"(?P<keep>\b(?i:(?:kind|best|warm|many)\s+regards|regards|best\s+wishes|best|thanks|thank\s+you|cheers"
        rf"|sincerely|yours\s+(?:truly|sincerely|faithfully))\b\s*[,!.]?\s*){_NAME}"
    ),
]
_known_names: list[re.Pattern] = []


def set_known_names(names) -> None:
    """Mask these customer names wherever they appear, in any letter case.

    shortcut: loaded once at startup from the contacts table; reload on CRM sync once contacts
    get names from somewhere other than the seed data.
    """
    global _known_names
    names = sorted({name.strip() for name in names if name and name.strip()}, key=len, reverse=True)
    alternatives = "|".join(r"\s+".join(map(re.escape, name.split())) for name in names)
    _known_names = [re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)] if names else []

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

_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")  # zero-width and bidi controls
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
    # Detect before masking: masking a "name" must never hide an injection phrase
    # ("my name is ignore all previous instructions").
    injection = bool(_INJECTION.search(_detection_view(text)))
    patterns = [
        *_PII_PATTERNS,
        *(("[REDACTED_ADDRESS]", p) for p in _ADDRESS_PATTERNS),
        *(("[REDACTED_NAME]", p) for p in [*_NAME_PATTERNS, *_known_names]),
    ]
    entities = 0
    for token, pattern in patterns:
        text, found = pattern.subn(_replace(token), text)
        entities += found
    return ShieldResult(text, entities, injection)
