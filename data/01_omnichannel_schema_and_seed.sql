-- ============================================================================
-- IQ Vector AI — Omnichannel AI Contact Center & MCP Engine (2-Day Pilot)
-- Starter SQLite Schema & Seed Data (Contacts, Conversations, Messages, Handoffs)
-- ============================================================================

PRAGMA foreign_keys = ON;

-- 1. CONTACTS (Cross-channel identity deduplication: email + phone + external_id + GDPR consent)
-- Maps directly to CONTRACT #2: IdentityLookup
CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    full_name TEXT,
    email TEXT UNIQUE,
    phone TEXT UNIQUE,
    telegram_handle TEXT UNIQUE,
    web_session_id TEXT,
    consent_gdpr INTEGER NOT NULL DEFAULT 1 CHECK (consent_gdpr IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2. CONVERSATIONS (Unified threads across Web / Telegram / Email / WhatsApp)
-- States: 'ai_active' | 'waiting_human' | 'resolved'
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
    state TEXT NOT NULL DEFAULT 'ai_active' CHECK (state IN ('ai_active', 'waiting_human', 'resolved')),
    last_channel TEXT NOT NULL,
    intent TEXT,
    sentiment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 3. MESSAGES (Canonical message log with webhook idempotency via message_id)
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY, -- Used for webhook idempotency / deduplication
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    channel TEXT NOT NULL CHECK (channel IN ('web', 'telegram', 'email', 'whatsapp')),
    role TEXT NOT NULL CHECK (role IN ('customer', 'ai', 'operator', 'system')),
    text TEXT NOT NULL, -- Stored AFTER BFSI PII redaction for customer messages
    tier_used TEXT CHECK (tier_used IN ('L0', 'L1', 'L2', NULL)),
    source_chunk_id TEXT,
    cost_usd REAL DEFAULT 0.0,
    latency_ms INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 4. HANDOFF EVENTS (Audit log of AI <-> Human transitions & lock reasons)
CREATE TABLE IF NOT EXISTS handoff_events (
    handoff_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    operator_id TEXT,
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================================
-- SAMPLE SEED DATA FOR TESTING CROSS-CHANNEL STITCHING, RAG & MCP TOOLS
-- ============================================================================

INSERT OR IGNORE INTO contacts (contact_id, full_name, email, phone, telegram_handle, web_session_id, consent_gdpr) VALUES
('cnt_001', 'Elena Rostova', 'elena.rostova@example.com', '+381641122334', '@elena_r', 'web_sess_991', 1),
('cnt_002', 'Marco Rossi', 'marco.rossi@example.com', '+393409988776', '@marcorossi', 'web_sess_992', 1),
('cnt_003', 'David Chen', 'david.chen@example.com', '+447700900123', '@davidchen_uk', 'web_sess_993', 0);

INSERT OR IGNORE INTO conversations (conversation_id, contact_id, state, last_channel, intent, sentiment) VALUES
('conv_001', 'cnt_001', 'ai_active', 'web', 'order_status', 'neutral'),
('conv_002', 'cnt_002', 'waiting_human', 'telegram', 'billing_dispute', 'frustrated');

INSERT OR IGNORE INTO messages (message_id, conversation_id, channel, role, text, tier_used, source_chunk_id, cost_usd, latency_ms, tokens_used, ts) VALUES
('msg_web_1001', 'conv_001', 'web', 'customer', 'Hi! Can you check the status of my order #ORD-7741? My email is [REDACTED_EMAIL]', NULL, NULL, 0.0, 0, 0, '2026-11-24T10:00:01Z'),
('msg_ai_1002',  'conv_001', 'web', 'ai',       'Hello Elena! Order #ORD-7741 is currently in_transit via DHL Express (tracking: DHL-994817263).', 'L0', 'ORD-7741', 0.0, 6, 0, '2026-11-24T10:00:02Z'),
('msg_tg_2001',  'conv_002', 'telegram', 'customer', 'I was charged twice for invoice #INV-902! Please connect me to a human agent immediately.', NULL, NULL, 0.0, 0, 0, '2026-11-24T10:12:00Z'),
('msg_ai_2002',  'conv_002', 'telegram', 'ai',       'I understand how frustrating a double charge is, Marco. I have escalated your thread to a human billing specialist and locked automated replies.', 'L0', 'faq_02', 0.0, 5, 0, '2026-11-24T10:12:01Z');

INSERT OR IGNORE INTO handoff_events (handoff_id, conversation_id, from_state, to_state, trigger_reason, operator_id, ts) VALUES
('hoff_001', 'conv_002', 'ai_active', 'waiting_human', 'mcp_tool:escalate_to_human (billing_dispute / customer requested human)', NULL, '2026-11-24T10:12:01Z');
