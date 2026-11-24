"""The MCP adapter, exercised over its real Streamable HTTP endpoint (JSON-RPC at /mcp)."""

from tests.cases import SPEC

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    response = client.post("/mcp", json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def call_tool(client, name, arguments):
    result = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert result["isError"] is False, result
    return result.get("structuredContent")


def test_both_tools_are_listed(client):
    names = {tool["name"] for tool in rpc(client, "tools/list")["tools"]}
    assert names == {"identity_and_order_lookup", "escalate_to_human"}


def test_lookup_returns_contract_2_and_reports_missing_consent(client):
    result = call_tool(client, "identity_and_order_lookup", {"email": "David.Chen@example.com", "order_id": "ord-9105"})
    assert set(result) == set(SPEC["contracts"]["CONTRACT_2_IdentityLookup_MCP"]["output_schema"])
    assert (result["contact_found"], result["contact_id"], result["consent_gdpr"]) == (True, "cnt_003", False)
    assert result["order"]["status"] == "delivered"
    assert "customer_email" not in result["order"]


def test_lookup_by_phone_and_unknown_values(client):
    by_phone = call_tool(client, "identity_and_order_lookup", {"phone": "+381 64 112 2334"})
    assert (by_phone["contact_id"], by_phone["consent_gdpr"], by_phone["order"]) == ("cnt_001", True, None)
    unknown = call_tool(client, "identity_and_order_lookup", {"email": "nobody@example.com", "order_id": "ORD-0000"})
    assert unknown == {"contact_found": False, "contact_id": None, "consent_gdpr": False, "order": None}


def test_escalate_tool_locks_the_conversation_and_logs_once(client, send):
    conversation_id = send("Hello!", user="mcp_user")["conversation_id"]
    first = rpc(client, "tools/call", {"name": "escalate_to_human", "arguments": {"conversation_id": conversation_id, "reason": "vip customer"}})
    again = rpc(client, "tools/call", {"name": "escalate_to_human", "arguments": {"conversation_id": conversation_id, "reason": "vip customer"}})
    assert '"handoff_logged": true' in first["content"][0]["text"]
    assert '"handoff_logged": false' in again["content"][0]["text"]

    transcript = client.get(f"/api/v1/conversations/{conversation_id}").json()
    assert transcript["conversation"]["state"] == "waiting_human"
    assert [h["trigger_reason"] for h in transcript["handoff_events"]] == ["vip customer"]


def test_escalate_reason_is_masked_before_storage(client, send):
    conversation_id = send("Hello!", user="mcp_pii_user")["conversation_id"]
    rpc(client, "tools/call", {"name": "escalate_to_human", "arguments": {"conversation_id": conversation_id, "reason": "call back on +44 7700 900123"}})
    reasons = [h["trigger_reason"] for h in client.get(f"/api/v1/conversations/{conversation_id}").json()["handoff_events"]]
    assert reasons == ["call back on [REDACTED_PHONE]"]


def test_escalating_an_unknown_conversation_is_a_tool_error(client):
    result = rpc(client, "tools/call", {"name": "escalate_to_human", "arguments": {"conversation_id": "conv_missing", "reason": "x"}})
    assert result["isError"] is True


def test_playground_is_served_and_never_renders_html_from_data(client):
    response = client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    # Customer text is untrusted: the page builds DOM nodes with textContent only.
    assert "innerHTML" not in response.text and "insertAdjacentHTML" not in response.text
