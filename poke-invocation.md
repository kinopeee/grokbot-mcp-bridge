# Calling Grok Bot from Poke — Spec and Procedures

English | [日本語](poke-invocation.ja.md)

Scope: Poke → MCP bridge → Grok Bot webhook → (callback) → bridge → Poke

---

## 1. Overview

```
Poke
  │  MCP tools/call (waits for the answer)
  ▼
grokbot-mcp-bridge (Fly)
  │  HTTP POST (asynchronous wake-up)
  ▼
Grok Bot routine with a webhook trigger
  │  ① Posts a short report to the Grok Bot chat
  │  ② POSTs the answer to callback_url
  ▼
Bridge resolves the waiting run by UUID
  │
  ▼
Returned to Poke as the MCP result
```

**Key constraint:** Grok Bot cannot put the answer in the webhook's HTTP response body.  
The webhook's HTTP 200 is only an ack meaning "accepted". The answer can be delivered to only two places:

1. The Grok Bot chat (report for the user)
2. A POST to the callback URL explicitly given in the payload

If Poke assumes "the answer comes back in the same HTTP response", this path cannot satisfy it. The bridge has to wait for the asynchronous callback and forward it to Poke's MCP `tools/call`.

---

## 2. Components

| Component | Role |
|------|------|
| **Poke** | Where the user talks. Calls the bridge as an MCP server |
| **Bridge** | `https://<app>.fly.dev` (`<app>` is `app` in `fly.toml`; defaults to `grokbot-mcp-bridge`) |
| **Grok Bot webhook** | Webhook trigger that wakes a Grok Bot routine (automation) |
| **Grok Bot** | Bot that answers webhook requests and sends callbacks. Its name, role, and tone follow the Grok Bot-side configuration; the bridge does not depend on them |

### Main bridge endpoints

| Path | Purpose |
|------|------|
| `GET /` | Service info (`mcp_streamable_http` / `mcp_sse` / `inbound_webhook`) |
| `GET /healthz` | Health check |
| `GET /docs` / `GET /openapi.json` | OpenAPI |
| `POST/GET /mcp` | MCP Streamable HTTP (Bearer required) |
| `/sse` | MCP SSE |
| `POST /hooks/grokbot` | Bridge's inbound Grok Bot webhook |
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

### Grok Bot routine

- Trigger: `{ "type": "webhook" }`
- Get the webhook URL / key / Authorization header from the routine settings in the Grok Bot app (do not write the values in docs)

### Ready-to-paste routine instructions

```
When triggered by the webhook, read the payload (webhook_event or equivalent) and headers.

Ignore silently (no chat message, no callback): MCP initialize / notifications, test: true / smoke_test / empty body, anything without a request text.
Otherwise treat message / text / query / prompt / content as the request.

Correlation ID (most important): the bridge waits on the UUID in run_id. Echo it unchanged. Never invent a new ID. Priority: run_id / runId → request_id / requestId / id → X-Run-Id / X-Request-Id headers. If there is no UUID, report "no UUID" in the chat.

Reply:
1. Post a short result to this chat.
2. If callback_url (or reply_url / response_url / reply_to / X-Poke-Callback-Url) exists, POST JSON to it; otherwise write "no callback URL".
3. Body: {"ok": true, "run_id": "<incoming UUID>", "request_id": "<same>", "answer": "<answer>", "message": "<answer>", "content": "<answer>", "text": "<answer>", "hasMore": false}. On failure: ok:false, error, hasMore:false, same run_id.
4. If callback_token / reply_token / X-Poke-Callback-Token / Authorization exists, send Authorization: Bearer <token>. Content-Type: application/json.
5. The chat report must include host, echoed run_id (UUID or not), HTTP status, response body summary. A numeric run_id in the response (e.g. 3) is the bridge's receipt number, not the waiting UUID.
Deliver only to this chat and the explicit callback URL. Do not put the answer in the webhook HTTP response body.
```

### Incoming requests to ignore silently (no message to the user, no callback)

- MCP handshakes such as `initialize` / notifications
- `test: true` / `smoke_test` / empty body
- Anything without a meaningful request text

### Incoming requests treated as real requests

Extract the request text from `message` / `text` / `query` / `prompt` / `content`, etc. The scope and content of the answer follow the Grok Bot-side configuration.

---

## 5. Callback (Grok Bot → bridge)

### How to find the callback URL (priority order)

From the payload or headers:

- `callback_url` / `reply_url` / `response_url` / `reply_to`
- `X-Poke-Callback-Url`, etc.

Never send to a guessed destination. If there is no URL, write "コールバックURLなし" (no callback URL) in the chat.

### POST body

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

- Host (e.g. `<app>.fly.dev`)
- The echoed `run_id` (and whether it is a UUID)
- HTTP status
- Summary of the response body (do not confuse the numeric receipt number with the waiting UUID)

---

## 6. Procedures (setup to end-to-end check)

### A. Grok Bot side (just verify if already done)

1. The routine with the webhook trigger is enabled
2. Copy the webhook URL / key / Authorization from the routine settings ("POST URL" → `CURSOR_WEBHOOK_URL`, "Key" → `CURSOR_WEBHOOK_API_KEY`)
3. Put them into the bridge secrets (`CURSOR_WEBHOOK_URL` / `CURSOR_WEBHOOK_API_KEY`)

### B. Bridge side

1. Register `https://<app>.fly.dev/mcp` (or `/sse`) as an MCP server in Poke
2. `ask_grokbot` issues a UUID4 per call and puts it in both `run_id` and `request_id` in the payload POSTed to the Grok Bot webhook (caller-supplied values are overwritten)
3. If the caller does not supply them, the bridge auto-attaches `callback_url` / `reply_url` / `response_url` (the token-scoped `/callbacks/{token}`)
4. After receiving the callback, resolve the wait for that UUID and return the text to Poke as the MCP `tools/call` result (if it does not arrive within `wait_seconds`, `pending` is returned; collect it with `wait_for_grokbot_answer`)

What Poke actually sends (`tools/call`):

```json
{"name": "ask_grokbot", "arguments": {"payload": {"message": "Introduce yourself briefly."}, "wait_seconds": 60}}
```

`payload` is forwarded to the Grok Bot webhook as-is (plus `run_id` / `request_id` / callback URLs), so put the request text in `message`. `wait_seconds` is clamped to 0–120. The result is a JSON string:

| `answer_status` | What Poke should do |
|---|---|
| `answered` | Show `answer_text` |
| `pending` | Call `wait_for_grokbot_answer` with the returned `run_id` (`timeout_seconds` up to 120); `summary` says so explicitly. `get_grokbot_run(run_id)` returns the same record without waiting |

If `ask_grokbot` returns `"error": "bridge_not_configured"`, the bridge is missing `CURSOR_WEBHOOK_URL` / `CURSOR_WEBHOOK_API_KEY` — check with `bridge_status` first.

### C. End-to-end test steps

1. **Handshake / empty test** → Grok Bot stays silent (expected)
2. Send a **real request** (e.g. "Introduce yourself") from Poke
3. A short answer and a callback report appear in the Grok Bot chat
4. Check that the callback returned `HTTP 200` and echoed the same UUID
5. Check that the answer appears in the Poke UI

### D. Troubleshooting table

| Observation | Likely location |
|------|----------|
| Nothing appears even in the Grok Bot chat | Webhook not reached / treated as a handshake and ignored / routine disabled |
| Appears in chat but the callback fails | URL, authentication, or body shape |
| Callback 200 with matching UUID, but nothing in Poke | **Bridge → Poke** (logs, MCP forwarding/display) |
| Callback 409 `already_answered` | That UUID is already answered. Retry with a new run |
| Response contains only a number like `run_id: 3` | Receipt number. Different from the waiting UUID |

Three things to check on the bridge operator side:

1. Is the callback for that `run_id` in the bridge logs?
2. If so, was the MCP `tools/call` result returned to Poke?
3. Is Poke displaying `answer_text` from the result (and, on `pending`, calling `wait_for_grokbot_answer` as `summary` instructs)?

### E. Common sticking point

Bridge → Poke MCP forwarding / display. In particular, if a run becomes `pending` after `wait_seconds` and Poke does not collect it with `wait_for_grokbot_answer`, the answer never shows up.

---

## 7. Operational notes

- Do not send anything outward (email, Slack, etc.) unless the payload explicitly asks for it
- Do not write secrets (webhook key / Bearer / MCP_API_KEY) in docs. Manage the values in the Grok Bot app settings and Fly secrets

---

## 8. Related links

- Bridge: `https://<app>.fly.dev/`
- Bridge OpenAPI: `https://<app>.fly.dev/openapi.json`
- Bridge spec: [SPEC.md](SPEC.md)
