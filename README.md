# IQ Vector Omnichannel Contact Center

A FastAPI service that takes customer messages from web chat, Telegram and email webhooks and handles each one through four stages:

1. **Shield.** Masks BFSI PII and blocks prompt injection before anything is stored.
2. **Route.** Sends the message through a 3-tier cascade: deterministic rules, a small LLM, then a frontier LLM or a human.
3. **Ground.** Answers only from the knowledge base, with the source chunk cited.
4. **Hand off.** Escalates to a human, which locks automated replies until an operator resolves the case.

It also includes:
- an MCP server with the two required tools,
- a playground UI at `/`,
- an evaluation harness covering the 30 brief cases plus 88 production-style edge cases.

- **Live demo:** https://iqvector-contact-center.onrender.com (playground) and [/docs](https://iqvector-contact-center.onrender.com/docs) (API). It runs on Render's free tier, so the first request after a quiet period can take about 50 seconds while the service wakes.
- **Results:** [EVAL_SCORECARD.md](EVAL_SCORECARD.md), from the deployed service with Groq answering.
- **Engineering decisions and trade-offs:** [docs/DECISIONS.md](docs/DECISIONS.md).

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt && uvicorn app.main:create_app --factory --port 8000
```

Open http://localhost:8000 for the playground and http://localhost:8000/docs for the API.
- The first start downloads the 30 MB embedding model.
- Without `GROQ_API_KEY`, the service still runs: grounded answers are returned verbatim from the knowledge base instead of being phrased by an LLM.

```bash
pytest                                            # 285 tests, no network needed
python eval_harness.py                            # 30 brief cases + edge cases, writes EVAL_SCORECARD.md
python eval_harness.py --base-url https://<host>  # same run against a deployed instance
```

The harness exits non-zero if any acceptance target or edge case fails.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | unset | Enables LLM phrasing on L1/L2. Without it, answers are verbatim KB text. |
| `GROQ_MODEL_L1` / `GROQ_MODEL_L2` | `openai/gpt-oss-20b` / `openai/gpt-oss-120b` | Model per tier |
| `OUTCOME_VALUE_USD` | `0.50` | Value of a resolved contact. LLM spend per turn is capped at 15% of it. |
| `DB_PATH` | `contact_center.db` | SQLite file. The schema and seed data load on startup. |
| `EMBEDDING_MODEL_DIR` | `models/potion-base-8M` | Local copy of the embedding model |

## How a message flows

```
POST /api/v1/message (CONTRACT #1)
  │
  ├─ shield            mask PII → [REDACTED_*], flag injection        app/shield.py
  ├─ tx 1              dedupe on message_id (scoped to the sender),   app/gateway.py
  │                    stitch identity, store the redacted message    app/identity.py
  ├─ route             L0 rules → L1 grounded FAQ → L2 multi-hop      app/router.py
  │                    or grounded refusal + human handoff            app/rag.py, app/llm.py
  └─ tx 2              state change, reply, idempotency receipt       app/tools.py
  │
  └─ CONTRACT #3 response
```

| Tier | Handles | Cost |
|---|---|---|
| **L0** | Injection blocks, small talk, order lookups by ID, support hours, and immediate escalations (billing disputes, requests for a human, refund/cancellation requests, address changes after dispatch, unknown order IDs) | $0, single-digit milliseconds |
| **L1** | One grounded FAQ, phrased by `gpt-oss-20b` | Fractions of a cent |
| **L2** | Questions that need several sources, phrased by `gpt-oss-120b`; or a grounded refusal that hands the conversation to a human | |

The answer-or-refuse decision is made in code: curated keyword anchors plus a calibrated semantic threshold. The LLM only rephrases text that is already grounded. If its answer fails a check, the source text is sent instead. Hallucination and PII targets therefore do not depend on model behaviour.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/message` | Webhook ingress: CONTRACT #1 in, CONTRACT #3 out. A retry with the same `message_id` returns the original response. |
| GET | `/api/v1/conversations/{id}` | Transcript and handoff audit log |
| POST | `/api/v1/conversations/{id}/takeover` | Operator claims the conversation; AI replies stop |
| POST | `/api/v1/conversations/{id}/operator_reply` | Operator message (masked before storage) |
| POST | `/api/v1/conversations/{id}/resolve` | Close the case; the customer's next message reopens it for the AI |
| POST | `/mcp` | MCP Streamable HTTP endpoint |

## MCP server

Two tools:
- `identity_and_order_lookup(email, phone, order_id)` returns CONTRACT #2 and reports `consent_gdpr`. The order comes back without the customer's email.
- `escalate_to_human(conversation_id, reason)` locks the conversation and logs the handoff.

The tools are served over HTTP at `/mcp`, or over stdio:

```json
{ "mcpServers": { "iqvector": { "command": "python", "args": ["-m", "app.mcp_server"], "cwd": "<repo path>" } } }
```

## Layout

```
app/           FastAPI app, shield, identity, router, retrieval, LLM client, tools, MCP server, playground
data/          the starter pack files, unchanged
eval/          production edge cases (88 messages)
tests/         unit, API, MCP and edge-case tests
eval_harness.py, EVAL_SCORECARD.md, docs/DECISIONS.md, render.yaml
```

## Known limits

**Webhook auth:** the webhook and operator endpoints are unauthenticated. Production would need:
- per-channel webhook signatures (Telegram secret token, Cloudflare-signed requests)
- operator SSO
- rate limiting

**Storage:** SQLite runs on Render's temporary disk and is re-seeded at every deploy. Use a persistent disk or Postgres for real use.

**Model and detection coverage:**
- Semantic matching uses an English model. Messages in other languages are handed to a human.
- Injection detection is pattern-based, so a novel phrasing can get past it. The LLM still only ever sees a grounded KB chunk, has no tools, and its numbers are checked against the source.
