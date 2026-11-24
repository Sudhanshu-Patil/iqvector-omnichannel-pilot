"""FastAPI app: webhook gateway, operator API, playground UI and the MCP endpoint.

Run with:  uvicorn app.main:create_app --factory
"""

import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from mcp.server.transport_security import TransportSecuritySettings

from app import llm, tools
from app.contracts import InboundWebhookEvent, OperatorAction, OperatorReply, RouterResponse
from app.db import DATA_DIR, connect, init_db, transaction
from app.gateway import MessageIdConflict, handle_message
from app.mcp_server import build_mcp_server
from app.rag import KnowledgeBase, load_model
from app.shield import shield

STATIC_DIR = Path(__file__).resolve().parent / "static"
KB_FILE = DATA_DIR / "02_mock_kb_orders_and_webhooks.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db_path = db_path or os.getenv("DB_PATH", "contact_center.db")
    init_db(db_path)
    kb = KnowledgeBase(json.loads(KB_FILE.read_text(encoding="utf-8"))["faq_kb"], load_model())

    mcp = build_mcp_server(db_path)
    # Stateless JSON mode: each tool call is self-contained, so any instance can serve it.
    # DNS-rebinding protection targets servers bound to localhost; this one is public by design.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="IQ Vector Omnichannel Contact Center",
        version="1.0.0",
        description="Webhook gateway with a BFSI PII shield, 3-tier routing, grounded RAG and human takeover.",
        lifespan=lifespan,
    )

    def get_conn():
        with closing(connect(db_path)) as conn:
            yield conn

    def require_conversation(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "conversation not found")
        return row

    @app.post(
        "/api/v1/message",
        response_model=RouterResponse,
        tags=["gateway"],
        responses={409: {"description": "message_id already used by a different sender"}},
    )
    def post_message(event: InboundWebhookEvent, conn: sqlite3.Connection = Depends(get_conn)):
        """CONTRACT #1 in, CONTRACT #3 out. Safe to retry: a repeated message_id from the same
        sender returns the original response."""
        try:
            return handle_message(conn, kb, event)
        except MessageIdConflict:
            raise HTTPException(409, "message_id already used by a different sender") from None

    @app.get("/api/v1/conversations/{conversation_id}", tags=["operator"])
    def get_conversation(conversation_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        """Transcript and handoff audit log. Contact PII is deliberately not exposed here."""
        conversation = require_conversation(conn, conversation_id)
        messages = conn.execute(
            """SELECT message_id, channel, role, text, tier_used, source_chunk_id, cost_usd, latency_ms, ts
               FROM messages WHERE conversation_id = ? ORDER BY rowid""",
            (conversation_id,),
        ).fetchall()
        handoffs = conn.execute(
            "SELECT * FROM handoff_events WHERE conversation_id = ? ORDER BY rowid", (conversation_id,)
        ).fetchall()
        return {
            "conversation": dict(conversation),
            "messages": [dict(m) for m in messages],
            "handoff_events": [dict(h) for h in handoffs],
        }

    @app.post("/api/v1/conversations/{conversation_id}/takeover", tags=["operator"])
    def takeover(conversation_id: str, body: OperatorAction, conn: sqlite3.Connection = Depends(get_conn)):
        """An operator claims the conversation: AI replies stop until it is resolved."""
        require_conversation(conn, conversation_id)
        return tools.escalate_to_human(conn, conversation_id, body.reason, body.operator_id)

    @app.post("/api/v1/conversations/{conversation_id}/operator_reply", tags=["operator"])
    def operator_reply(conversation_id: str, body: OperatorReply, conn: sqlite3.Connection = Depends(get_conn)):
        with transaction(conn):
            conversation = require_conversation(conn, conversation_id)
            if conversation["state"] != "waiting_human":
                raise HTTPException(409, "take over the conversation before replying")
            message_id = "op_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO messages (message_id, conversation_id, channel, role, text) VALUES (?, ?, ?, 'operator', ?)",
                (message_id, conversation_id, conversation["last_channel"], shield(body.text).redacted_text),
            )
        return {"message_id": message_id, "conversation_id": conversation_id, "state": "waiting_human"}

    @app.post("/api/v1/conversations/{conversation_id}/resolve", tags=["operator"])
    def resolve(conversation_id: str, body: OperatorAction, conn: sqlite3.Connection = Depends(get_conn)):
        require_conversation(conn, conversation_id)
        return tools.resolve(conn, conversation_id, body.operator_id)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # RENDER_GIT_COMMIT is set by Render on every deploy; it lets a remote eval name the exact code it ran against.
        return {"ok": True, "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7], "llm": {"enabled": llm.enabled(), **llm.MODELS}}

    @app.get("/", include_in_schema=False)
    def playground():
        return FileResponse(STATIC_DIR / "index.html")

    # Mounted last so every route above takes precedence; the MCP app serves /mcp.
    app.mount("/", mcp_app)
    return app
