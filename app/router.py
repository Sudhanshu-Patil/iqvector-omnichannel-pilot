"""3-tier routing cascade.

L0  deterministic rules, $0: lock, injection block, small talk, escalation triggers, order
    lookups, support hours.
L1  one grounded FAQ, phrased by a small model.
L2  multi-hop answers (several sources) by a frontier model, or a grounded refusal that hands
    the conversation to a human.

Whether to answer is always decided here, in code. The LLM only phrases replies that are
already grounded, so refusals and sources never depend on model behaviour.
"""

import re
from dataclasses import dataclass
from typing import Optional

from app import llm, tools
from app.rag import KnowledgeBase, tokenize

# "ORD-7741", "ord7741", "ORD 7741", "order #7741", "order number 7741". 3-6 digits so a typo
# like ORD-77411 gets an explicit "not found" instead of being ignored.
ORDER_ID = re.compile(r"\bORD[-_ #]?(\d{3,6})\b|\border\s*(?:no\.?|number|num|#)\s*[:#]?\s*(\d{4})\b", re.IGNORECASE)
PII_TOKEN = re.compile(r"\[REDACTED_[A-Z]+\]")
SENSITIVE_TOKEN = re.compile(r"\[REDACTED_(?:CARD|ID)\]")

HUMAN_REQUEST = re.compile(
    r"\b(?:connect|transfer|talk|speak|chat|put|escalate)\b[^.!?\n]{0,20}"
    r"\b(?:human|agent|person|operator|specialist|representative|someone|manager|supervisor)\b"
    r"|\b(?:want|need|get)\s+(?:a\s+|an\s+|to\s+)?(?:human|agent|operator|real\s+person|manager|supervisor)\b"
    r"|\b(?:real|actual)\s+(?:person|human)\b|\bcall\s+me\b"
    r"|^\W*(?:human|agent|operator|representative|manager|supervisor|real\s+person)(?:\s+(?:please|pls|plz|now|asap))*\W*$"
    r"|\b(?:not|don'?t|do\s+not|no\s+more)\b[^.!?\n]{0,25}\b(?:bot|robot|machine|chatbot)s?\b",
    re.IGNORECASE,
)
# "I don't need a human, just tell me ..." is not a handoff request.
NEGATED_HUMAN_REQUEST = re.compile(
    r"\b(?:don'?t|do\s+not|no\s+need\s+(?:for|to)|never)\b[^.!?\n]{0,20}\b(?:human|agent|person|operator)\b",
    re.IGNORECASE,
)
# faq_02's keywords miss most real wording ("charged me 2x", "unauthorized charge", "pending hold").
BILLING_ISSUE = re.compile(
    r"\b(?:charged|billed|debited)\b[^.!?\n]{0,15}\b(?:twice|two\s+times|2x|double|again|wrong|too\s+much|incorrectly)\b"
    r"|\b(?:double|duplicate)[-\s]?(?:charged|billed|charges?|payments?|transactions?)\b"
    r"|\bover-?(?:charged|billed)\b"
    r"|\b(?:unrecogni[sz]ed|unauthori[sz]ed|unknown|fraudulent|wrong)\s+(?:charges?|payments?|transactions?|debits?)\b"
    r"|\b(?:pending|authori[sz]ation|duplicate)\s+(?:hold|authori[sz]ation)s?\b"
    r"|\bcharge-?backs?\b|\bdispute\b",
    re.IGNORECASE,
)
# Requests a human has to handle: refunds and cancellations the assistant cannot perform, and
# questions about one customer's refund, which the KB cannot answer.
ACTION_REQUEST = re.compile(
    r"\b(?:cancel|terminate)\s+(?:my\s+|the\s+|this\s+|an?\s+|our\s+)?(?:order|subscription|licen[cs]e|purchase|plan|contract)\b"
    r"|\b(?:i\s+want|i\s+need|i\s+would\s+like|i'd\s+like|i\s+demand|please|pls|plz|can\s+you|could\s+you)\b"
    r"[^.!?\n]{0,25}\b(?:refund|reimburse|cancel|money\s+back)\b"
    r"|\brefund\s+(?:me|my|it|this|that|the\s+order)\b|\bprocess\s+(?:a|my|the)\s+refund\b"
    r"|\bfor\s+(?:the|my|a|this)\s+(?:\w+\s+)?refund\b"
    r"|\b(?:where|when)\b[^.!?\n]{0,25}\bmy\s+(?:refund|money)\b|\brefund\s+status\b",
    re.IGNORECASE,
)
# The KB lists "DHL" as an faq_01 keyword, so with an order in hand the address rule needs an
# explicit change-of-address intent; otherwise "where is my DHL order?" would escalate.
ADDRESS_CHANGE = re.compile(
    r"\b(?:change|update|modify|correct|edit)\b[^.!?\n]{0,30}\baddress\b|\breroute\b|\bredirect\b"
    r"|\b(?:wrong|new|different|another|other)\s+(?:\w+\s+)?address\b",
    re.IGNORECASE,
)
GREETING = re.compile(r"^\W*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))(?:\s+there)?\W*$", re.IGNORECASE)
ACKNOWLEDGEMENT = re.compile(
    r"^\W*(?:thanks?|thank\s+you|thx|ty|ok(?:ay)?|k|cool|great|perfect|awesome|nice|got\s+it|understood|noted"
    r"|bye|goodbye|cheers)(?:\s+(?:a\s+lot|so\s+much|very\s+much|again|bye))*\W*$",
    re.IGNORECASE,
)
ARE_YOU_A_BOT = re.compile(r"^\W*(?:are|r)\s+(?:you|u)\s+(?:a\s+)?(?:bot|robot|machine|ai|human|real(?:\s+person)?)\W*$", re.IGNORECASE)

# FAQs whose policy is an action rather than an answer.
ADDRESS_FAQ = "faq_01"  # address changes after dispatch need a human operator
BILLING_FAQ = "faq_02"  # billing disputes "must be immediately escalated to a human billing specialist"
REFUND_FAQ = "faq_03"  # governs refunds and cancellations, which a human processes
HOURS_FAQ = "faq_07"  # static fact, answered at L0

LOCKED_REPLY = (
    "Thanks, I've added this to your case. A human specialist is handling this conversation "
    "and will reply here shortly."
)
INJECTION_REPLY = (
    "I can't help with that request. I can help with your orders, billing, refunds, payment "
    "methods, API limits and data privacy."
)
GREETING_REPLY = (
    "Hello! I can help with order status, refunds, billing, payment methods, API limits and "
    "data privacy. What can I do for you?"
)
ACK_REPLY = "Glad I could help. Is there anything else you need?"
CLARIFY_REPLY = (
    "Could you tell me a bit more about what you need? I can help with order status, refunds, "
    "billing, payment methods, API limits and data privacy."
)
HELP_REPLY = "How can I help you today?"
BOT_REPLY = (
    "I'm IQ Vector's automated assistant. I can help with orders, refunds, billing, payment "
    "methods, API limits and data privacy, and I can connect you with a human specialist at any time."
)
SENSITIVE_NOTICE = (
    "For your security I've masked the card, ID or password details in your message. "
    "Please don't share them in chat."
)
HUMAN_REPLY = "Of course. I've passed your conversation to a human specialist, who will reply here shortly."
BILLING_REPLY = (
    "I'm sorry about the billing problem. Billing disputes are handled by our human billing "
    "specialists, so I've escalated your case and one of them will reply here shortly."
)
ACTION_REPLY = (
    "I can't process refunds or cancellations in chat, so I've passed your request to a "
    "specialist who will follow up here."
)
UNKNOWN_ORDER_REPLY = (
    "I couldn't find {orders} in our system, so I won't guess. I've passed this to a human "
    "specialist who will look into it."
)
ADDRESS_LOCKED_REPLY = (
    "Order {order_id} is already {status} with {carrier}, so its address can't be changed "
    "automatically. I've escalated this to a human operator who can request a reroute with the carrier."
)
ADDRESS_CHANGEABLE_REPLY = (
    "Order {order_id} is still {status}, so its delivery address can still be changed. I can't edit "
    "orders in chat, so I've passed your request to a specialist who will update it and confirm here."
)
REFUSAL_REPLY = (
    "I don't have verified information to answer that, so I won't guess. I've passed your "
    "question to a human specialist, who will follow up here."
)
FAILSAFE_REPLY = (
    "Sorry, something went wrong on our side. I've passed your conversation to a human "
    "specialist so it doesn't get lost."
)

_STATUS_WORDING = {"in_transit": "in transit", "processing": "being processed", "delivered": "delivered"}


@dataclass
class Decision:
    level: str
    reply: str
    source: Optional[str] = None
    cost_usd: float = 0.0
    tokens: int = 0
    escalate_reason: Optional[str] = None  # set: the conversation moves to waiting_human
    locked: bool = False  # a system notice: the conversation already belongs to a human


def order_summary(order: dict) -> str:
    status = _STATUS_WORDING.get(order["status"], order["status"])
    tracking = order["tracking_number"] or "not assigned yet"
    eta = order["eta"] if order["eta"].startswith("Delivered") else f"Estimated delivery: {order['eta']}"
    return f"Order {order['order_id']} is {status} with {order['carrier']} (tracking: {tracking}). {eta}."


def order_ids(text: str) -> list[str]:
    return list(dict.fromkeys("ORD-" + (m.group(1) or m.group(2)) for m in ORDER_ID.finditer(text)))


def decide(kb: KnowledgeBase, text: str, *, injection: bool, state: str, llm_allowed: bool) -> Decision:
    """Route one redacted customer message. `text` must already have passed the shield."""
    decision = _route(kb, text, injection, state, llm_allowed)
    if SENSITIVE_TOKEN.search(text):
        decision.reply = f"{SENSITIVE_NOTICE}\n\n{decision.reply}"
    return decision


def _route(kb: KnowledgeBase, text: str, injection: bool, state: str, llm_allowed: bool) -> Decision:
    if state == "waiting_human":
        return Decision("L0", LOCKED_REPLY, locked=True)
    if injection:
        return Decision("L0", INJECTION_REPLY)

    # Masked tokens carry no meaning for routing ("[REDACTED_CARD]" must not look like a payment question).
    content = PII_TOKEN.sub(" ", text)
    if not re.search(r"[^\W_]", content):  # only PII, emoji or punctuation
        if PII_TOKEN.search(text):
            return Decision("L0", HELP_REPLY)
        return Decision("L0", CLARIFY_REPLY if "?" in content else ACK_REPLY)
    if ACKNOWLEDGEMENT.match(content):
        return Decision("L0", ACK_REPLY)
    if GREETING.match(content):
        return Decision("L0", GREETING_REPLY)
    if ARE_YOU_A_BOT.match(content):
        return Decision("L0", BOT_REPLY)

    anchors = kb.anchored(content)
    if BILLING_FAQ in anchors or BILLING_ISSUE.search(content):
        return Decision("L0", BILLING_REPLY, BILLING_FAQ, escalate_reason="billing_dispute")
    if HUMAN_REQUEST.search(content) and not NEGATED_HUMAN_REQUEST.search(content):
        return Decision("L0", HUMAN_REPLY, escalate_reason="customer_requested_human")
    if ACTION_REQUEST.search(content):
        reply = f"{kb.answers[REFUND_FAQ]}\n\n{ACTION_REPLY}"
        return Decision("L0", reply, REFUND_FAQ, escalate_reason="refund_or_cancellation_request")

    ids = order_ids(content)
    if ids:
        # An address sent alongside an order ID ("send ORD-8812 to [REDACTED_ADDRESS]") is a change request.
        wants_address_change = bool(ADDRESS_CHANGE.search(content)) or "[REDACTED_ADDRESS]" in text
        return _orders(kb, content, ids, anchors, llm_allowed, wants_address_change)

    if anchors == [HOURS_FAQ]:
        return Decision("L0", kb.answers[HOURS_FAQ], HOURS_FAQ)

    if not anchors:
        match = kb.semantic_match(content)
        if match == BILLING_FAQ:
            return Decision("L0", BILLING_REPLY, BILLING_FAQ, escalate_reason="billing_dispute")
        anchors = [match] if match else []
    if not anchors:
        if PII_TOKEN.search(text) and len(tokenize(content)) <= 2:
            return Decision("L0", HELP_REPLY)  # "my email is [REDACTED_EMAIL]": details, but no question yet
        return Decision("L2", REFUSAL_REPLY, escalate_reason="no_grounded_answer")
    return _answer(content, {faq: kb.answers[faq] for faq in anchors}, "L1" if len(anchors) == 1 else "L2", llm_allowed)


def _orders(
    kb: KnowledgeBase, content: str, ids: list[str], anchors: list[str], llm_allowed: bool, wants_address_change: bool
) -> Decision:
    found = {order_id: order for order_id in ids if (order := tools.find_order(order_id))}
    missing = [order_id for order_id in ids if order_id not in found]

    # Address changes always need a human: either a carrier reroute or an edit the assistant can't make.
    if wants_address_change and found:
        locked = next((o for o in found.values() if not o["can_change_address"]), None)
        if locked:
            reply = ADDRESS_LOCKED_REPLY.format(
                order_id=locked["order_id"], status=_STATUS_WORDING.get(locked["status"], locked["status"]), carrier=locked["carrier"]
            )
            return Decision("L0", reply, ADDRESS_FAQ, escalate_reason="address_change_after_dispatch")
        order = next(iter(found.values()))
        reply = ADDRESS_CHANGEABLE_REPLY.format(order_id=order["order_id"], status=_STATUS_WORDING.get(order["status"], order["status"]))
        return Decision("L0", reply, f"{order['order_id']},{ADDRESS_FAQ}", escalate_reason="address_change_request")

    summaries = [order_summary(order) for order in found.values()]
    if missing:
        orders = ("orders " if len(missing) > 1 else "order ") + ", ".join(missing)
        reply = "\n".join([*summaries, UNKNOWN_ORDER_REPLY.format(orders=orders)])
        return Decision("L0", reply, ",".join(found) or None, escalate_reason="unknown_order_id")

    policies = [faq for faq in anchors if faq != ADDRESS_FAQ]
    if not policies:
        match = kb.semantic_match(content)  # e.g. "can I still get a refund for ORD-9105?"
        policies = [match] if match and match != ADDRESS_FAQ else []
    if policies:  # the orders plus a policy question: multi-hop
        context = {order_id: order_summary(order) for order_id, order in found.items()}
        context.update({faq: kb.answers[faq] for faq in policies})
        return _answer(content, context, "L2", llm_allowed)
    return Decision("L0", "\n".join(summaries), ",".join(found))


def _answer(text: str, context: dict[str, str], level: str, llm_allowed: bool) -> Decision:
    source = ",".join(context)
    phrased = llm.phrase(level, text, context) if llm_allowed else None
    spent = (phrased.cost_usd, phrased.tokens) if phrased else (0.0, 0)
    # No key, no consent, over budget, or the model's answer failed a check: the grounded source
    # text itself is the answer. A rejected answer was still billed, so its cost is reported.
    reply = phrased.answer if phrased and phrased.answer else "\n\n".join(context.values())
    return Decision(level, reply, source, *spent)
