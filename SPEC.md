# Grok Bot Invocation Spec and Procedures (grokbot-mcp-bridge)

English | [日本語](SPEC.ja.md)

Communication spec for Poke ⇄ bridge ⇄ Grok Bot (running on a Cursor automation webhook).

## 1. Overview

```
Poke ──(MCP / Bearer)──▶ Bridge ──(POST, Bearer crsr_…)──▶ Grok Bot (Cursor automation webhook)
Poke ◀──(MCP response)── Bridge ◀──(POST callback_url)──── Grok Bot
```

- MCP endpoints: `https://<app>.fly.dev/mcp` (Streamable HTTP) / `/sse` (SSE). `<app>` is `app` in `fly.toml` (defaults to `grokbot-mcp-bridge`)
- Authentication: `Authorization: Bearer <MCP_API_KEY>` (`Authorization: <key>` and `X-API-Key: <key>` are also accepted)
- The Grok Bot webhook URL and API key are held only as server-side secrets on the bridge (Fly secrets) and are never returned to Poke.

## 2. MCP tools

| Tool | Purpose |
|---|---|
| `bridge_status` | Check configuration status (reports only whether secrets are set, never their values) |
| `ask_grokbot(payload, wait_seconds=45)` | Send a request to Grok Bot. Waits for the callback for 45 seconds by default (max 120) and returns `answer_text`. `wait_seconds=0` returns immediately |
| `get_grokbot_run(run_id)` | Fetch a run by UUID (raw `answer` plus `answer_text`) |
| `wait_for_grokbot_answer(run_id, timeout_seconds=60)` | Wait for the callback of an unanswered run (max 120 seconds) |
| `list_grokbot_runs(limit)` | List recent runs |
| `list_grokbot_events(limit)` / `get_grokbot_event(event_id)` | List inbound webhook events / fetch one in full |

Resources: `grokbot://runs`, `grokbot://events`

## 3. Outbound request (bridge → Grok Bot)

JSON that `ask_grokbot` POSTs to the Grok Bot webhook:

```json
{
  "message": "<content from Poke; the payload is passed through as-is>",
  "run_id":      "8b1c…-uuid4",
  "request_id":  "8b1c…-uuid4",
  "callback_url": "https://<app>.fly.dev/callbacks/<token>",
  "reply_url":    "…same URL…",
  "response_url": "…same URL…"
}
```

- The bridge always sets `run_id` and `request_id` to **the same UUID4** (overwriting any caller-supplied value).
- `callback_url` / `reply_url` / `response_url` are added automatically unless the caller provides them.
- Header: `Authorization: Bearer <CURSOR_WEBHOOK_API_KEY>` (added automatically; no Poke-side configuration needed).

## 4. Callback (Grok Bot → bridge)

When the answer is ready, Grok Bot POSTs to the `callback_url` it received:

```json
{"ok": true, "answer": "answer text", "run_id": "<the received run_id, echoed as-is>"}
```

- **Echoing the UUID is required**: include the received UUID in either `run_id` or `request_id`.
- Send to `POST /callbacks/<token>` (matched on both token and UUID).
- The answer is extracted from the first present field in the order `answer → message → content → text → output → result` and normalized into `answer_text` (if `content` is an array of `{"type":"text","text":…}`, the texts are joined with newlines).
- The body must be a JSON object, at most 256 KB.

| Situation | Response |
|---|---|
| Success | `200 {"ok":true,"run_id":…}` |
| No UUID | `400 {"error":"run_id_required"}` |
| Token and UUID do not match | `400 {"error":"run_id_mismatch"}` |
| Unknown token / unknown UUID | `404` |
| Already answered (duplicate) | `409` |
| Expired (default 3600 seconds) | `410` |

## 5. Example response as seen by Poke

```json
{
  "ok": true,
  "run_id": "06eec502-cf7b-468d-8307-8dcf945e1f17",
  "answer_status": "answered",
  "answer_text": "…answer text…",
  "summary": "Grok Bot answered: …"
}
```

If no answer arrives within 45 seconds, the bridge returns `answer_status: "pending"` with
`"summary": "No answer yet from Grok Bot; call wait_for_grokbot_answer with run_id …"`, and Poke should then call `wait_for_grokbot_answer`.

## 6. Inbound webhook (Grok Bot → bridge, push delivery)

`POST /hooks/grokbot`
- Authentication: `X-Webhook-Signature: sha256=<HMAC-SHA256(body, INBOUND_WEBHOOK_SECRET)>` or `Authorization: Bearer <INBOUND_WEBHOOK_SECRET>`
- Optional replay protection: when `X-Webhook-Timestamp: <unix seconds>` is sent, the signature must be computed over `"<ts>." + body` and the timestamp must be within `INBOUND_TIMESTAMP_TOLERANCE_SECONDS` (default 300) of the server clock. Without the header, the legacy body-only signature is still accepted.
- The body is limited to 256 KB; larger bodies return `413 {"error":"body_too_large"}`.
- If the body contains `callback_url` (or `reply_url` / `response_url`), the bridge POSTs `{"ok":true,"answer":"受信しました (event_id=N)"}` ("received") to it, but only to public https URLs on hosts listed in `CALLBACK_ALLOWED_HOSTS` (SSRF guard: the allowlist is required — unset means every callback URL is rejected — and private, loopback, link-local, the bridge's own host, and ports other than 80/443 are always rejected). `CALLBACK_ALLOW_HTTP=1` only permits the `http` scheme; it does not relax the allowlist, IP-range, or port checks.
- Otherwise it returns `{"ok":true,"event_id":N,"callback":"none","note":"コールバックURLなし"}` ("no callback URL").

## 7. Environment variables (Fly secrets)

| Variable | Source | Notes |
|---|---|---|
| `CURSOR_WEBHOOK_URL`, `CURSOR_WEBHOOK_API_KEY` | Copied from the Cursor automation that runs Grok Bot (webhook URL and `crsr_…` key) | Required. Never returned to Poke |
| `MCP_API_KEY` | Generated by the operator (e.g. `openssl rand -hex 32`) | Required. The same value goes into Poke's API Key field. Unset → `/mcp` returns 503 |
| `INBOUND_WEBHOOK_SECRET` | Generated by the operator | Optional. Unset → `/hooks/grokbot` returns 503; `ask_grokbot` is unaffected |
| `ALLOWED_HOSTS` | `<app>.fly.dev` | Required on Fly (DNS-rebinding protection and default for `PUBLIC_BASE_URL`) |
| `DB_PATH` | `/data/bridge.db` | Defaults to `/data/bridge.db` in the image (Dockerfile `ENV`) and can be overridden; on Fly it points at volume `bridge_data` |
| `CALLBACK_ALLOWED_HOSTS` | Comma-separated hostnames the inbound `callback_url` may target | Optional, but unset → every inbound `callback_url` is rejected (fail closed) |
| `PUBLIC_BASE_URL`, `CALLBACK_TTL_SECONDS`, `CALLBACK_ALLOW_HTTP`, `INBOUND_TIMESTAMP_TOLERANCE_SECONDS` | — | Optional |

## 8. Operations

```bash
uv run --extra dev pytest -q               # tests
flyctl apps create <app>                   # first time only
flyctl volumes create bridge_data -r <region> -s 1 -a <app> --yes   # first time only (mounted at /data)
flyctl secrets set -a <app> KEY=value      # set / update secrets (section 7)
flyctl deploy --remote-only --ha=false     # deploy (app / region come from fly.toml)
flyctl logs -a <app>                       # logs for run created / callback resolved / trigger returning
```

Poke-side setup: use the `/mcp` URL as the MCP URL and put the value of `MCP_API_KEY` in the API Key field (without the "Bearer" prefix). Poke re-fetches tool definitions automatically after tool names change.
