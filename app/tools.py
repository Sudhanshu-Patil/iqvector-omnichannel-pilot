"""Order lookup and the conversation state machine.

These functions back the two MCP tools and are called in-process by the router: MCP is the
external interface to the same logic, not a network hop the app makes to itself.
"""

import json
import sqlite3
import uuid
from typing import Optional

from app.db import DATA_DIR, transaction
from app.identity import normalize_email, normalize_phone
from app.shield import shield

_KB_FILE = DATA_DIR / "02_mock_kb_orders_and_webhooks.json"
ORDERS = {order["order_id"]: order for order in json.loads(_KB_FILE.read_text(encoding="utf-8"))["orders"]}


def find_order(order_id: str) -> Optional[dict]:
    """The order without customer_email: the owner's address never leaves this layer."""
    order = ORDERS.get(order_id.strip().upper())
    return {key: value for key, value in order.items() if key != "customer_email"} if order else None


def identity_and_order_lookup(
    conn: sqlite3.Connection,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    order_id: Optional[str] = None,
) -> dict:
    """CONTRACT #2: contact by email or phone (with GDPR consent) and/or an order by ID."""
    contact = None
    if email:
        contact = conn.execute(
            "SELECT contact_id, consent_gdpr FROM contacts WHERE email = ?", (normalize_email(email),)
        ).fetchone()
    if contact is None and phone:
        contact = conn.execute(
            "SELECT contact_id, consent_gdpr FROM contacts WHERE phone = ?", (normalize_phone(phone),)
        ).fetchone()
    return {
        "contact_found": contact is not None,
        "contact_id": contact["contact_id"] if contact else None,
        "consent_gdpr": bool(contact and contact["consent_gdpr"]),
        "order": find_order(order_id) if order_id else None,
    }


def _log_handoff(conn, conversation_id: str, from_state: str, to_state: str, reason: str, operator_id: Optional[str]):
    conn.execute(
        """INSERT INTO handoff_events (handoff_id, conversation_id, from_state, to_state, trigger_reason, operator_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        # Reasons can arrive as free text from MCP clients, so they get masked like everything else stored.
        ("hoff_" + uuid.uuid4().hex[:12], conversation_id, from_state, to_state, shield(reason).redacted_text[:200], operator_id),
    )


def _transition(conn, conversation_id: str, to_state: str, reason: str, operator_id: Optional[str]) -> dict:
    with transaction(conn):
        row = conn.execute("SELECT state FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown conversation_id: {conversation_id}")
        changed = row["state"] != to_state
        if changed:
            conn.execute(
                "UPDATE conversations SET state = ?, updated_at = datetime('now') WHERE conversation_id = ?",
                (to_state, conversation_id),
            )
            _log_handoff(conn, conversation_id, row["state"], to_state, reason, operator_id)
    return {"conversation_id": conversation_id, "state": to_state, "handoff_logged": changed}


def escalate_to_human(conn: sqlite3.Connection, conversation_id: str, reason: str, operator_id: Optional[str] = None) -> dict:
    """Lock the conversation for a human (AI replies stop) and log the handoff. Idempotent."""
    return _transition(conn, conversation_id, "waiting_human", reason, operator_id)


def resolve(conn: sqlite3.Connection, conversation_id: str, operator_id: str) -> dict:
    """Close the conversation. The customer's next message reopens it for the AI."""
    return _transition(conn, conversation_id, "resolved", "operator_resolved", operator_id)
