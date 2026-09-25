# grokbot-mcp-bridge

English | [日本語](README.ja.md)

Secure MCP bridge to a Grok Bot running through a Cursor automation webhook.
Poke can call the bridge without receiving the webhook URL or API key.

## Documentation

| Document | English | 日本語 |
|---|---|---|
| Overview (this file) | [README.md](README.md) | [README.ja.md](README.ja.md) |
| Bridge specification and operations | [SPEC.md](SPEC.md) | [SPEC.ja.md](SPEC.ja.md) |
| Calling Eric (Grok Bot) from Poke | [poke-invocation.md](poke-invocation.md) | [poke-invocation.ja.md](poke-invocation.ja.md) |

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
flyctl deploy --remote-only --ha=false
python3 -m pytest -q
```
