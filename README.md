# grokbot-mcp-bridge

English | [日本語](README.ja.md)

Secure MCP bridge to a Grok Bot running through a Cursor automation webhook.
Poke can call the bridge without receiving the webhook URL or API key.

## Documentation

| Document | English | 日本語 |
|---|---|---|
| Overview (this file) | [README.md](README.md) | [README.ja.md](README.ja.md) |
| Bridge specification and operations | [SPEC.md](SPEC.md) | [SPEC.ja.md](SPEC.ja.md) |
| Calling Grok Bot from Poke | [poke-invocation.md](poke-invocation.md) | [poke-invocation.ja.md](poke-invocation.ja.md) |

## Quick start (Poke → Grok Bot in four steps)

`<app>` below is `app` in `fly.toml` (defaults to `grokbot-mcp-bridge`).

### 1. Prepare the Grok Bot webhook

In the Cursor automation that runs Grok Bot, enable the webhook trigger and copy its URL and `crsr_…` API key. They become `CURSOR_WEBHOOK_URL` / `CURSOR_WEBHOOK_API_KEY` and are stored only on the bridge — Poke never sees them.

### 2. Deploy the bridge to Fly.io

```bash
flyctl apps create <app>
flyctl volumes create bridge_data -r <region> -s 1 -a <app> --yes   # SQLite lives on /data
flyctl secrets set -a <app> \
  CURSOR_WEBHOOK_URL="$CURSOR_WEBHOOK_URL" \
  CURSOR_WEBHOOK_API_KEY="$CURSOR_WEBHOOK_API_KEY" \
  MCP_API_KEY="$(openssl rand -hex 32)" \
  INBOUND_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
  ALLOWED_HOSTS=<app>.fly.dev \
  DB_PATH=/data/bridge.db
flyctl deploy --remote-only --ha=false -a <app>
```

- `MCP_API_KEY` and `INBOUND_WEBHOOK_SECRET` are **not** issued by Poke or Cursor; you generate them yourself. Keep the generated `MCP_API_KEY` — Poke needs the same value in step 3, and Fly does not show secret values again.
- `INBOUND_WEBHOOK_SECRET` is optional. Without it, `POST /hooks/grokbot` returns 503 and `bridge_status` reports `inbound_webhook_secret_configured: false`; `ask_grokbot` still works.

### 3. Connect Poke

Add an MCP integration in Poke with:

| Field | Value |
|---|---|
| Server URL | `https://<app>.fly.dev/mcp` (Streamable HTTP). Use `https://<app>.fly.dev/sse` if the client only supports SSE |
| API Key | the value of `MCP_API_KEY` (no `Bearer ` prefix) |

### 4. Verify end to end

1. `GET https://<app>.fly.dev/healthz` → `200 {"ok":true}`
2. From Poke, call `bridge_status` → every `*_configured` field is `true`
3. From Poke, call `ask_grokbot` with `payload={"message": "Introduce yourself briefly."}`, `wait_seconds=60`
   - `answer_status: "answered"` → `answer_text` holds the reply
   - `answer_status: "pending"` → call `wait_for_grokbot_answer(run_id, timeout_seconds=120)`
4. `flyctl logs -a <app>` shows `run created` → `callback resolved` → `trigger returning`

For the Grok Bot-side contract (echoing `run_id`, posting to `callback_url`) and troubleshooting, see [poke-invocation.md](poke-invocation.md).

## Endpoints

- `GET /healthz`
- `GET /`
- `POST /mcp` (authenticated MCP)
- `GET /sse` and `POST /messages/` (authenticated MCP)
- `POST /hooks/grokbot` (signed inbound Grok Bot events)
- `POST /callbacks` and `POST /callbacks/{token}` (Grok Bot answers)

## MCP tools

- `bridge_status`
- `ask_grokbot`
- `get_grokbot_run`
- `wait_for_grokbot_answer`
- `list_grokbot_runs`
- `list_grokbot_events`
- `get_grokbot_event`

Resources are available at `grokbot://events` and `grokbot://runs`.

## Environment variables

`CURSOR_WEBHOOK_URL`, `CURSOR_WEBHOOK_API_KEY`, `MCP_API_KEY`,
`INBOUND_WEBHOOK_SECRET`, `DB_PATH`, `ALLOWED_HOSTS`, `PUBLIC_BASE_URL`,
`CALLBACK_TTL_SECONDS`, `CALLBACK_ALLOW_HTTP`, and `CALLBACK_ALLOWED_HOSTS`.

## Callback contract

Grok Bot should POST JSON such as:

```json
{"ok": true, "answer": "...", "run_id": "<echoed uuid>"}
```

The `run_id` (or `request_id`) must echo the UUID sent by `ask_grokbot`.
When an inbound webhook has no callback URL, the bridge returns
`「コールバックURLなし」` ("no callback URL") and records the event without
delivering a callback.

## Commands

```bash
uv run --extra dev pytest -q               # tests
uv lock && uv export --no-dev --format requirements-txt --no-emit-project -o requirements.txt   # update pinned deps
flyctl deploy --remote-only --ha=false     # deploy (app / region come from fly.toml)
```
