"""SQLite access: one short-lived connection per unit of work, explicit write transactions."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SCHEMA_FILE = DATA_DIR / "01_omnichannel_schema_and_seed.sql"

# Our only addition to the provided schema: an idempotency-key store. It records who sent each
# inbound message_id and the exact response returned, so a redelivered webhook gets the same
# answer instead of a re-run, and only ever to the sender that first used the id.
# response_json stays NULL while the first delivery is still being processed.
RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS webhook_receipts (
    message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    sender TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str) -> sqlite3.Connection:
    # Autocommit mode; atomic work goes through transaction(). check_same_thread is off because
    # FastAPI may run a request's dependency and handler on different pool threads.
    conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL + NORMAL: commits skip the fsync (a power cut can lose the last commits, never corrupt
    # the file). That keeps L0 turns inside the 10 ms budget.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str) -> None:
    with closing(connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.executescript(RECEIPTS_DDL)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so read-then-write sequences (dedupe check,
    contact creation, state transitions) cannot interleave with another request. Re-entrant: an
    inner call joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
