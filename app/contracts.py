"""Pydantic models for the contracts in data/03_contracts_and_30_eval_cases.json."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["web", "telegram", "email", "whatsapp"]
Level = Literal["L0", "L1", "L2"]
ConversationState = Literal["ai_active", "waiting_human", "resolved"]


class IdentityHints(BaseModel):
    email: Optional[str] = Field(default=None, max_length=254)
    phone: Optional[str] = Field(default=None, max_length=32)
    handle: Optional[str] = Field(default=None, max_length=64)


class InboundWebhookEvent(BaseModel):
    """CONTRACT #1. Size caps bound regex work and LLM spend per request; whitespace-only text is rejected."""

    model_config = ConfigDict(str_strip_whitespace=True)

    channel: Channel
    external_user_id: str = Field(min_length=1, max_length=254)
    message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    identity_hints: IdentityHints = IdentityHints()
    ts: datetime


class RouterResponse(BaseModel):
    """CONTRACT #3, returned by POST /api/v1/message and stored per turn."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    message_id: str
    duplicate_webhook_ignored: bool
    pii_redacted_text: str
    pii_entities_detected: int
    prompt_injection_blocked: bool
    level_used: Level
    source_chunk_id: Optional[str]
    reply: str
    escalate: bool
    conversation_state: ConversationState
    cost_usd: float
    latency_ms: int


class IdentityLookupResult(BaseModel):
    """CONTRACT #2 output of the identity_and_order_lookup MCP tool."""

    contact_found: bool
    contact_id: Optional[str]
    consent_gdpr: bool
    order: Optional[dict]


class OperatorAction(BaseModel):
    operator_id: str = Field(default="op_demo", min_length=1, max_length=64)
    reason: str = Field(default="operator_takeover", min_length=1, max_length=200)


class OperatorReply(BaseModel):
    operator_id: str = Field(default="op_demo", min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
