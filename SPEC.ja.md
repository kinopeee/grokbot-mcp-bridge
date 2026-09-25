# Grok Bot 呼び出し仕様・手順 (grokbot-mcp-bridge)

[English](SPEC.md) | 日本語

Poke ⇄ ブリッジ ⇄ Grok Bot（Cursor automation webhook 上で稼働）の通信仕様まとめ。

## 1. 全体像

```
Poke ──(MCP / Bearer)──▶ ブリッジ ──(POST, Bearer crsr_…)──▶ Grok Bot (Cursor automation webhook)
Poke ◀──(MCP 応答)────── ブリッジ ◀──(POST callback_url)──── Grok Bot
```

- MCP エンドポイント: `https://<app>.fly.dev/mcp`（Streamable HTTP）/ `/sse`（SSE）。`<app>` は `fly.toml` の `app`（既定 `grokbot-mcp-bridge`）
- 認証: `Authorization: Bearer <MCP_API_KEY>`（`Authorization: <key>` / `X-API-Key: <key>` も可）
- Grok Bot 側の webhook URL / API キーはブリッジのサーバー側シークレット（Fly secrets）にのみ保持され、Poke には一切返さない。

## 2. MCP ツール

| ツール | 用途 |
|---|---|
| `bridge_status` | 設定状態の確認（シークレットの有無のみ、値は返さない） |
| `ask_grokbot(payload, wait_seconds=45)` | Grok Bot に問い合わせ。既定 45 秒（最大 120）コールバックを待って `answer_text` を返す。`wait_seconds=0` で即時返却 |
| `get_grokbot_run(run_id)` | run を UUID で取得（`answer` 生データ + `answer_text`） |
| `wait_for_grokbot_answer(run_id, timeout_seconds=60)` | 未回答 run のコールバック到着を待つ（最大 120 秒） |
| `list_grokbot_runs(limit)` | 最近の run 一覧 |
| `list_grokbot_events(limit)` / `get_grokbot_event(event_id)` | 受信 webhook イベントの一覧 / 全文 |

リソース: `grokbot://runs`, `grokbot://events`

## 3. 送信仕様（ブリッジ → Grok Bot）

`ask_grokbot` が Grok Bot の webhook に POST する JSON:

```json
{
  "message": "<Poke からの内容。payload はそのまま透過>",
  "run_id":      "8b1c…-uuid4",
  "request_id":  "8b1c…-uuid4",
  "callback_url": "https://<app>.fly.dev/callbacks/<token>",
  "reply_url":    "…同じ URL…",
  "response_url": "…同じ URL…"
}
```

- `run_id` と `request_id` は **同一の UUID4** をブリッジが必ず付与（呼び出し側が指定しても上書き）。
- `callback_url` / `reply_url` / `response_url` は呼び出し側が指定しなければ自動付与。
- ヘッダー: `Authorization: Bearer <CURSOR_WEBHOOK_API_KEY>`（自動付与。Poke 側設定不要）。

## 4. コールバック仕様（Grok Bot → ブリッジ）

Grok Bot は回答が用意できたら、受け取った `callback_url` に POST する:

```json
{"ok": true, "answer": "回答本文", "run_id": "<受け取った run_id をそのままエコー>"}
```

- **UUID のエコーは必須**: `run_id` または `request_id` のどちらかに、受け取った UUID を含める。
- 送り先は `POST /callbacks/<token>`（推奨。token + UUID の両方で照合）または `POST /callbacks`（UUID のみで照合）。
- 回答フィールドは `answer → message → content → text → output → result` の順で抽出し `answer_text` に正規化（`content` が `{"type":"text","text":…}` 配列なら改行連結）。
- 本文は JSON オブジェクト、最大 256 KB。

| 状況 | 応答 |
|---|---|
| 正常 | `200 {"ok":true,"run_id":…}` |
| UUID なし | `400 {"error":"run_id_required"}` |
| token と UUID が不一致 | `400 {"error":"run_id_mismatch"}` |
| 不明 token / 不明 UUID | `404` |
| 既に回答済み（重複） | `409` |
| 期限切れ（既定 3600 秒） | `410` |

## 5. Poke 側から見た応答例

```json
{
  "ok": true,
  "run_id": "06eec502-cf7b-468d-8307-8dcf945e1f17",
  "answer_status": "answered",
  "answer_text": "…回答本文…",
  "summary": "Grok Bot answered: …"
}
```

45 秒以内に届かなければ `answer_status: "pending"` と
`"summary": "No answer yet from Grok Bot; call wait_for_grokbot_answer with run_id …"` を返すので、Poke は `wait_for_grokbot_answer` を呼ぶ。

## 6. 受信 webhook（Grok Bot → ブリッジ、能動配信）

`POST /hooks/grokbot`
- 認証: `X-Webhook-Signature: sha256=<HMAC-SHA256(body, INBOUND_WEBHOOK_SECRET)>` または `Authorization: Bearer <INBOUND_WEBHOOK_SECRET>`
- 本文に `callback_url`（または `reply_url` / `response_url`）があれば、公開 https URL のみ（SSRF ガード: private/loopback/link-local/自ホスト等は拒否）に `{"ok":true,"answer":"受信しました (event_id=N)"}` を POST。
- ない場合は `{"ok":true,"event_id":N,"callback":"none","note":"コールバックURLなし"}` を返す。

## 7. 環境変数（Fly secrets）

| 変数 | 出所 | 備考 |
|---|---|---|
| `CURSOR_WEBHOOK_URL`, `CURSOR_WEBHOOK_API_KEY` | Grok Bot が動く Cursor automation から控える（webhook URL と `crsr_…` キー） | 必須。Poke には返さない |
| `MCP_API_KEY` | 運用者が生成（例: `openssl rand -hex 32`） | 必須。同じ値を Poke の API Key 欄に入れる。未設定なら `/mcp` は 503 |
| `INBOUND_WEBHOOK_SECRET` | 運用者が生成 | 任意。未設定なら `/hooks/grokbot` は 503（`ask_grokbot` には影響なし） |
| `ALLOWED_HOSTS` | `<app>.fly.dev` | Fly では必須（DNS rebinding 対策と `PUBLIC_BASE_URL` の既定値） |
| `DB_PATH` | `/data/bridge.db` | Fly では必須（ボリューム `bridge_data`） |
| `PUBLIC_BASE_URL`, `CALLBACK_TTL_SECONDS`, `CALLBACK_ALLOW_HTTP`, `CALLBACK_ALLOWED_HOSTS` | — | 任意 |

## 8. 運用手順

```bash
uv run --extra dev pytest -q               # テスト
flyctl apps create <app>                   # 初回のみ
flyctl volumes create bridge_data -r <region> -s 1 -a <app> --yes   # 初回のみ（/data にマウント）
flyctl secrets set -a <app> KEY=value      # シークレットの設定・更新（7 章）
flyctl deploy --remote-only --ha=false     # デプロイ（app / region は fly.toml）
flyctl logs -a <app>                       # run created / callback resolved / trigger returning のログ
```

Poke 側の設定: MCP URL に `/mcp`、API Key 欄に `MCP_API_KEY` の値（"Bearer" は付けない）。ツール名変更後は Poke が自動で再取得する。
