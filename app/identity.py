"""Cross-channel identity stitching.

Only identifiers supplied by the channel integration are trusted (webhook identity_hints and the
channel's own user id). Emails or phone numbers typed inside a message are never used: they are
unverified, and matching on them would let anyone attach themselves to another customer's thread.
"""

import re
import sqlite3
import uuid

from app.contracts import IdentityHints

# Which contacts column holds each channel's native user id. Fixed strings, safe to interpolate.
CHANNEL_ID_COLUMN = {
    "web": "web_session_id",
    "telegram": "telegram_handle",
    "email": "email",
    "whatsapp": "phone",
}


def normalize_email(value: str | None) -> str | None:
    return value.strip().lower() if value and "@" in value else None


def normalize_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return "+" + digits if digits else None


def normalize_handle(value: str | None) -> str | None:
    handle = (value or "").strip().lstrip("@").lower()
    return "@" + handle if handle else None


_NORMALIZE = {
    "email": normalize_email,
    "phone": normalize_phone,
    "telegram_handle": normalize_handle,
    "web_session_id": lambda v: v.strip() or None,
}


def resolve_contact(conn: sqlite3.Connection, channel: str, external_user_id: str, hints: IdentityHints) -> sqlite3.Row:
    """Find the contact behind a message, creating one if needed. Caller holds a write transaction."""
    native_column = CHANNEL_ID_COLUMN[channel]
    # Match priority: email, phone, handle, then the channel's own id. First hit wins; contacts
    # are never merged automatically.
    candidates = [
        ("email", normalize_email(hints.email)),
        ("phone", normalize_phone(hints.phone)),
        ("telegram_handle", normalize_handle(hints.handle)),
        (native_column, _NORMALIZE[native_column](external_user_id)),
    ]
    candidates = [(column, value) for column, value in candidates if value]

    contact = None
    for column, value in candidates:
        contact = conn.execute(f"SELECT * FROM contacts WHERE {column} = ?", (value,)).fetchone()
        if contact:
            break

    if contact is None:
        contact_id = "cnt_" + uuid.uuid4().hex
        values = dict(reversed(candidates))  # on duplicate columns the higher-priority value wins
        columns = ", ".join(["contact_id", *values])
        placeholders = ", ".join("?" * (len(values) + 1))
        conn.execute(f"INSERT INTO contacts ({columns}) VALUES ({placeholders})", (contact_id, *values.values()))
        return conn.execute("SELECT * FROM contacts WHERE contact_id = ?", (contact_id,)).fetchone()

    # Remember new channel ids on the matched contact so the next message stitches even without
    # hints. Only empty fields are filled; an id already owned by another contact is left alone.
    for column, value in candidates:
        if contact[column] is None:
            try:
                conn.execute(
                    f"UPDATE contacts SET {column} = ?, updated_at = datetime('now') WHERE contact_id = ?",
                    (value, contact["contact_id"]),
                )
            except sqlite3.IntegrityError:
                pass
    return conn.execute("SELECT * FROM contacts WHERE contact_id = ?", (contact["contact_id"],)).fetchone()


def resolve_conversation(conn: sqlite3.Connection, contact_id: str, channel: str) -> sqlite3.Row:
    """One unified thread per contact: reuse the latest conversation, reopening it if resolved."""
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE contact_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
        (contact_id,),
    ).fetchone()
    if conversation is None:
        conversation_id = "conv_" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO conversations (conversation_id, contact_id, last_channel) VALUES (?, ?, ?)",
            (conversation_id, contact_id, channel),
        )
    else:
        conversation_id = conversation["conversation_id"]
        conn.execute(
            """UPDATE conversations
               SET last_channel = ?,
                   state = CASE state WHEN 'resolved' THEN 'ai_active' ELSE state END,
                   updated_at = datetime('now')
               WHERE conversation_id = ?""",
            (channel, conversation_id),
        )
    return conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
