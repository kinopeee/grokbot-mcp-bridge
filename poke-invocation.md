# Calling Eric (Grok Bot) from Poke — Spec and Procedures

English | [日本語](poke-invocation.ja.md)

Last updated: 2026-09-26 (Asia/Tokyo)  
Scope: Poke → MCP bridge → Grok Bot webhook → (callback) → bridge → Poke

---

## 1. Overview

```
Poke
  │  MCP tools/call (waits for the answer)
  ▼
cursor-mcp-bridge (Fly)
  │  HTTP POST (asynchronous wake-up)
  ▼
Grok Bot routine "Web webhook"
  │  ① Posts a short report in Japanese to this chat
  │  ② POSTs the answer to callback_url
  ▼
Bridge resolves the waiting run by UUID
  │
  ▼
Returned to Poke as the MCP result (※ this step is sometimes not completed)
```

**Key constraint:** Grok Bot cannot put the answer in the webhook's HTTP response body.  
The webhook's HTTP 200 is only an ack meaning "accepted". The answer can be delivered to only two places:

1. This Eric chat (report for the user)
2. A POST to the callback URL explicitly given in the payload

If Poke assumes "the answer comes back in the same HTTP response", this path cannot satisfy it. The bridge has to wait for the asynchronous callback and forward it to Poke's MCP `tools/call`.

---

## 2. Components

| Component | Role |
|------|------|
| **Poke** | Where the user talks. Calls the bridge as an MCP server |
| **Bridge** | `https://cursor-mcp-bridge-kinopee.fly.dev` (service name `cursor-mcp-bridge`) |
| **Grok Bot webhook** | Entry point that wakes the routine "Web webhook" (folder: `web-webhook`) |
| **Eric** | Project-operations bot. Does no specialist work. Answers webhook requests and sends callbacks |

### Main bridge endpoints

| Path | Purpose |
|------|------|
| `GET /` | Service info (`mcp_streamable_http` / `mcp_sse` / `inbound_webhook`) |
| `GET /healthz` | Health check |
| `GET /docs` / `GET /openapi.json` | OpenAPI |
| `POST/GET /mcp` | MCP Streamable HTTP (Bearer required) |
| `/sse` | MCP SSE |
| `POST /hooks/grokbot` | Bridge's inbound Grok Bot webhook |
| `POST /callbacks` | Receives answers matched by correlation UUID only |
| `POST /callbacks/{token}` | Token-scoped callback |

---

## 3. Synchronization contract (correlation ID)

Poke and the bridge wait on a **UUID-formatted `run_id` (or `request_id`)**.

| Do | Don't |
|----------------|----------------------|
| Echo the UUID from the incoming payload as-is | Issue a new ID yourself |
| Put the same UUID in both `run_id` and `request_id` | Use test labels (e.g. `grok-test-...`) for correlation |
| Treat a numeric `run_id: 3` in a response as the bridge's receipt number | Try to resolve the wait with a numeric receipt number |

Fields to read, in priority order (UUID-like values first):

1. `run_id` / `runId`
2. `request_id` / `requestId` / `id`
3. Headers such as `X-Run-Id` / `X-Request-Id`
4. Only when there is no UUID, fall back to a string ID and write "UUIDなし" (no UUID) in the chat

Example successful callback response (from the bridge):

```json
{"ok":true,"run_id":"<same UUID as the incoming request>"}
```

Resending to a run that has already been answered may return `HTTP 409` / `already_answered` (expected).

---

## 4. Incoming webhook (bridge → Grok Bot)

### Routine

- Name: **Web webhook**
- folder: `web-webhook`
- Trigger: `{ "type": "webhook" }`
- Settings links (in the app):
  - [Webhook URL](grokbot://app/v1/sidebar?target=webhook-url&automation=web-webhook)
  - [Webhook key](grokbot://app/v1/sidebar?target=webhook-key&automation=web-webhook)
  - [Authorization header](grokbot://app/v1/sidebar?target=webhook-header&automation=web-webhook)

### Incoming requests to ignore silently (no message to the user, no callback)

- MCP handshakes such as `initialize` / notifications
- `test: true` / `smoke_test` / empty body
- Anything without a meaningful request text

### Incoming requests treated as real requests

Extract the request text from `message` / `text` / `query` / `prompt` / `content`, etc.

Eric's scope:

- Project operations, staffing, and checking unassigned / Blocked items
- Does not do the specialist work itself (returns a plan to hand it to Eng, etc. when needed)

---

## 5. Callback (Grok Bot → bridge)

### How to find the callback URL (priority order)

From the payload or headers:

- `callback_url` / `reply_url` / `response_url` / `reply_to`
- `X-Poke-Callback-Url`, etc.

Never send to a guessed destination. If there is no URL, write "コールバックURLなし" (no callback URL) in the chat.

### POST body (current format)

Always include the near-required fields (multiple keys are sent so both the bridge and Poke can read them):

```json
{
  "ok": true,
  "run_id": "<incoming UUID as-is>",
  "request_id": "<same>",
  "answer": "<answer text>",
  "message": "<answer text>",
  "content": "<answer text>",
  "text": "<answer text>",
  "hasMore": false
}
```

On failure:

```json
{
  "ok": false,
  "run_id": "<incoming UUID as-is>",
  "request_id": "<same>",
  "error": "<reason>",
  "hasMore": false
}
```

### Authentication

If the payload or headers contain `callback_token` / `reply_token` / `X-Poke-Callback-Token` / `Authorization`:

```
Authorization: Bearer <token>
Content-Type: application/json
```

Otherwise, POST without a Bearer token.

### Items the chat report must include

- Host (e.g. `cursor-mcp-bridge-kinopee.fly.dev`)
- The echoed `run_id` (and whether it is a UUID)
- HTTP status
- Summary of the response body (do not confuse the numeric receipt number with the waiting UUID)

---

## 6. Procedures (setup to end-to-end check)

### A. Grok Bot side (just verify if already done)

1. The routine "Web webhook" is enabled
2. Copy the [Webhook URL](grokbot://app/v1/sidebar?target=webhook-url&automation=web-webhook) / key / Authorization
3. Put that URL (and the key / Bearer if needed) into the bridge configuration

### B. Bridge side

1. Register `https://cursor-mcp-bridge-kinopee.fly.dev/mcp` (or `/sse`) as an MCP server in Poke
2. On tool calls, include **the same UUID being waited on** in the payload POSTed to the Grok Bot webhook, as `run_id` (recommended)
3. Include `callback_url` (or the token-scoped `/callbacks/{token}`)
4. After receiving the callback, resolve the wait for that UUID and return the text to Poke as the MCP `tools/call` result

### C. End-to-end test steps

1. **Handshake / empty test** → Eric stays silent (expected)
2. Send a **real request** (e.g. "What's your name?") from Poke
3. A short Japanese answer and a callback report appear in the Eric chat
4. Check that the callback returned `HTTP 200` and echoed the same UUID
5. Check that the answer appears in the Poke UI

### D. Troubleshooting table

| Observation | Likely location |
|------|----------|
| Nothing appears even in the Eric chat | Webhook not reached / treated as a handshake and ignored / routine disabled |
| Appears in chat but the callback fails | URL, authentication, or body shape |
| Callback 200 with matching UUID, but nothing in Poke | **Bridge → Poke** (logs, MCP forwarding, which key is read) |
| Callback 409 `already_answered` | That UUID is already answered. Retry with a new run |
| Response contains only a number like `run_id: 3` | Receipt number. Different from the waiting UUID |

Three things to ask the Poke / Devin side to check:

1. Is the callback for that `run_id` in the bridge logs?
2. If so, was the MCP `tools/call` result returned to Poke?
3. Which JSON key is read for the answer text? (Currently `answer` / `message` / `content` / `text` are all sent.)

---

## 7. Current status (as of 2026-09-26)

| Segment | Status |
|------|------|
| Poke → bridge → Grok Bot webhook | Real requests are arriving |
| Grok Bot → chat report | Working |
| Grok Bot → bridge callback (UUID echo, HTTP 200) | Working |
| Bridge → display in the Poke UI | **Often unresolved** (forwarding or which key is read) |

---

## 8. Operational notes

- Do not send anything outward (email, Slack, etc.) unless the payload explicitly asks for it
- Create new specialist bots only when existing ones are insufficient, and only after user approval
- Example dev members: Eng Mgr, Eng 1–5, (for plugins / API wrappers) tinkabot
- This document is an operational memo. Do not write secrets (webhook key / Bearer) here; look up values via the app's sidebar links

---

## 9. Related links

- Routine webhook URL: [Webhook URL](grokbot://app/v1/sidebar?target=webhook-url&automation=web-webhook)
- Routine webhook key: [Webhook key](grokbot://app/v1/sidebar?target=webhook-key&automation=web-webhook)
- Authorization: [Authorization header](grokbot://app/v1/sidebar?target=webhook-header&automation=web-webhook)
- Bridge: `https://cursor-mcp-bridge-kinopee.fly.dev/`
- Bridge OpenAPI: `https://cursor-mcp-bridge-kinopee.fly.dev/openapi.json`
