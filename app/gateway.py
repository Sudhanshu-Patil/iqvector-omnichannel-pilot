"""Webhook ingestion for POST /api/v1/message: shield -> dedupe + identity -> route -> persist."""

import logging
import sqlite3
import time
import uuid

from app import llm, tools
from app.contracts import InboundWebhookEvent, RouterResponse
from app.db import transaction
from app.identity import resolve_contact, resolve_conversation
from app.rag import KnowledgeBase
from app.router import FAILSAFE_REPLY, Decision, decide
from app.shield import shield

log = logging.getLogger(__name__)


class MessageIdConflict(Exception):
    """A message_id already used by a different sender: not a retry, and never replayed."""


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def handle_message(conn: sqlite3.Connection, kb: KnowledgeBase, event: InboundWebhookEvent) -> RouterResponse:
    started = time.perf_counter()
    shielded = shield(event.text)
    sender = f"{event.channel}:{event.external_user_id}"

    # Transaction 1: the dedupe check, the customer row and the idempotency receipt are written
    # under one write lock, so two concurrent deliveries of the same webhook cannot both get
    # through, and a duplicate is caught before any identity write or LLM call.
    with transaction(conn):
        existing = conn.execute(
            """SELECT m.conversation_id, m.text, r.sender, r.response_json
               FROM messages m LEFT JOIN webhook_receipts r ON r.message_id = m.message_id
               WHERE m.message_id = ?""",
            (event.message_id,),
        ).fetchone()
        if existing is not None:
            # Replaying to anyone but the original sender would hand them another customer's answer.
            if existing["sender"] != sender:
                raise MessageIdConflict(event.message_id)
            return _replay(conn, event.message_id, existing, started)
        hints = event.identity_hints
        if event.channel == "email" and not hints.email:
            hints = hints.model_copy(update={"email": event.external_user_id})  # the sender address
        contact = resolve_contact(conn, event.channel, event.external_user_id, hints)
        conversation = resolve_conversation(conn, contact["contact_id"], event.channel)
        conn.execute(
            "INSERT INTO messages (message_id, conversation_id, channel, role, text, ts) VALUES (?, ?, ?, 'customer', ?, ?)",
            (event.message_id, conversation["conversation_id"], event.channel, shielded.redacted_text, event.ts.isoformat()),
        )
        conn.execute(
            "INSERT INTO webhook_receipts (message_id, conversation_id, sender) VALUES (?, ?, ?)",
            (event.message_id, conversation["conversation_id"], sender),
        )

    conversation_id = conversation["conversation_id"]
    try:
        decision = decide(
            kb,
            shielded.redacted_text,
            injection=shielded.injection,
            state=conversation["state"],
            # No third-party model sees a customer who has not given GDPR consent.
            llm_allowed=llm.enabled() and bool(contact["consent_gdpr"]),
        )
    except Exception:
        # Fail toward a human: the message is already stored, so it must not go unanswered.
        log.exception("routing failed for message_id=%s; escalating", event.message_id)
        decision = Decision("L2", FAILSAFE_REPLY, escalate_reason="internal_error_failsafe")

    # Transaction 2: the state change, the reply and the idempotency receipt commit together.
    with transaction(conn):
        if decision.escalate_reason:
            tools.escalate_to_human(conn, conversation_id, decision.escalate_reason)
        state = conn.execute(
            "SELECT state FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()["state"]
        latency_ms = _elapsed_ms(started)
        conn.execute(
            """INSERT INTO messages (message_id, conversation_id, channel, role, text, tier_used, source_chunk_id,
                                     cost_usd, latency_ms, tokens_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "ai_" + uuid.uuid4().hex,  # own id space, so a client message_id can never collide with it
                conversation_id,
                event.channel,
                "system" if decision.locked else "ai",
                decision.reply,
                decision.level,
                decision.source,
                decision.cost_usd,
                latency_ms,
                decision.tokens,
            ),
        )
        response = RouterResponse(
            conversation_id=conversation_id,
            message_id=event.message_id,
            duplicate_webhook_ignored=False,
            pii_redacted_text=shielded.redacted_text,
            pii_entities_detected=shielded.pii_entities,
            prompt_injection_blocked=shielded.injection,
            level_used=decision.level,
            source_chunk_id=decision.source,
            reply=decision.reply,
            escalate=state == "waiting_human",
            conversation_state=state,
            cost_usd=decision.cost_usd,
            latency_ms=latency_ms,
        )
        conn.execute(
            "UPDATE webhook_receipts SET response_json = ? WHERE message_id = ?",
            (response.model_dump_json(), event.message_id),
        )
    log.info(
        "message_id=%s level=%s source=%s escalate=%s latency_ms=%s cost_usd=%s",
        event.message_id, decision.level, decision.source, response.escalate, latency_ms, decision.cost_usd,
    )
    return response


def _replay(conn: sqlite3.Connection, message_id: str, duplicate: sqlite3.Row, started: float) -> RouterResponse:
    """A redelivered webhook gets the original response back: no new rows, no LLM call."""
    if duplicate["response_json"]:
        original = RouterResponse.model_validate_json(duplicate["response_json"])
        return original.model_copy(update={"duplicate_webhook_ignored": True, "latency_ms": _elapsed_ms(started)})

    # The first delivery is still being processed: acknowledge without running it twice.
    state = conn.execute(
        "SELECT state FROM conversations WHERE conversation_id = ?", (duplicate["conversation_id"],)
    ).fetchone()["state"]
    return RouterResponse(
        conversation_id=duplicate["conversation_id"],
        message_id=message_id,
        duplicate_webhook_ignored=True,
        pii_redacted_text=duplicate["text"],
        pii_entities_detected=duplicate["text"].count("[REDACTED_"),
        prompt_injection_blocked=False,
        level_used="L0",
        source_chunk_id=None,
        reply="",
        escalate=state == "waiting_human",
        conversation_state=state,
        cost_usd=0.0,
        latency_ms=_elapsed_ms(started),
    )
