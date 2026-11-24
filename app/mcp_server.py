"""MCP adapter for the two contact-center tools.

Served over Streamable HTTP at /mcp by the FastAPI app, or over stdio for desktop clients:
    python -m app.mcp_server
"""

import os
from contextlib import closing
from typing import Optional

from mcp.server.mcpserver import MCPServer

from app import tools
from app.contracts import IdentityLookupResult
from app.db import connect, init_db


def build_mcp_server(db_path: str) -> MCPServer:
    server = MCPServer(
        name="iqvector-contact-center",
        instructions=(
            "Tools for the IQ Vector contact center. Look up a customer and their GDPR consent, "
            "or an order by ID; hand a conversation to a human specialist."
        ),
    )

    @server.tool()
    def identity_and_order_lookup(
        email: Optional[str] = None, phone: Optional[str] = None, order_id: Optional[str] = None
    ) -> IdentityLookupResult:
        """Find a contact by email or phone (returns contact_id and consent_gdpr) and/or an order
        by ID such as ORD-7741. The order is returned without the customer's email."""
        with closing(connect(db_path)) as conn:
            return IdentityLookupResult(**tools.identity_and_order_lookup(conn, email, phone, order_id))

    @server.tool()
    def escalate_to_human(conversation_id: str, reason: str) -> dict:
        """Move a conversation to 'waiting_human' (automated replies stop) and log the handoff
        in handoff_events. Calling it again on a waiting conversation changes nothing."""
        with closing(connect(db_path)) as conn:
            return tools.escalate_to_human(conn, conversation_id, reason)

    return server


if __name__ == "__main__":
    path = os.getenv("DB_PATH", "contact_center.db")
    init_db(path)
    build_mcp_server(path).run("stdio")
