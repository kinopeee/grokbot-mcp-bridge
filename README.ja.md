# grokbot-mcp-bridge

[English](README.md) | 日本語

Cursor automation の webhook 上で動く Grok Bot につなぐ、セキュアな MCP ブリッジです。
Poke は webhook の URL や API キーを受け取らずにブリッジを呼び出せます。

## ドキュメント

| ドキュメント | English | 日本語 |
|---|---|---|
| 概要（このファイル） | [README.md](README.md) | [README.ja.md](README.ja.md) |
| ブリッジの仕様と運用 | [SPEC.md](SPEC.md) | [SPEC.ja.md](SPEC.ja.md) |
| Poke から Grok Bot を呼び出す | [poke-invocation.md](poke-invocation.md) | [poke-invocation.ja.md](poke-invocation.ja.md) |

## クイックスタート（Poke → Grok Bot を 4 ステップで）

以下の `<app>` は `fly.toml` の `app`（既定は `grokbot-mcp-bridge`）。

### 0. どの値をどこに入れるか

| 値 | どこで取得するか | どこに入れるか |
|---|---|---|
| `CURSOR_WEBHOOK_URL` | Grok Bot アプリ → **Web webhook** トリガのルーチン → 「POST先」（`https://api2.cursor.sh/automations/webhook/…`） | ブリッジの Fly secret（手順 2） |
| `CURSOR_WEBHOOK_API_KEY` | 同じ画面 → 「キー」（`crsr_…`）。`Authorization: Bearer` の行ではなくキーの値だけをコピー | ブリッジの Fly secret（手順 2） |
| `MCP_API_KEY` | 自分で生成（`openssl rand -hex 32`） | Fly secret（手順 2）**と** Poke → New Integration → 「API Key」（手順 3）— 両方に同じ値 |
| `INBOUND_WEBHOOK_SECRET` | 自分で生成（任意） | Fly secret（手順 2）、`POST /hooks/grokbot` を使う場合は Grok Bot 側のプッシュルーチンにも |
| `https://<app>.fly.dev/mcp` | ブリッジ固定 | Poke → New Integration → 「Server URL」（手順 3） |
| Grok Bot ルーチンの指示 | このリポジトリ: [poke-invocation.ja.md § 4](poke-invocation.ja.md#4-webhook-着信ブリッジ--grok-bot)（貼るだけテンプレート） | Grok Bot アプリ → 同じルーチン → 「指示」欄 |

ルーチン画面: チャット見出し → 情報パネル → Routines → Web webhook.

### 1. Grok Bot 側の webhook を用意する

Grok Bot を動かしている Cursor automation で webhook トリガーを有効にし、URL と `crsr_…` API キーを控える。これが `CURSOR_WEBHOOK_URL` / `CURSOR_WEBHOOK_API_KEY` になり、ブリッジ側にだけ保存される（Poke には渡らない）。

ルーチンの「指示」欄には、Grok Bot が `run_id` をエコーして `callback_url` に回答を POST するよう書く必要がある。[poke-invocation.ja.md § 4](poke-invocation.ja.md#4-webhook-着信ブリッジ--grok-bot) のテンプレートをそのまま貼る。

### 2. ブリッジを Fly.io にデプロイする

`flyctl auth login` でログインするか、非対話利用なら https://fly.io/tokens でトークンを作成して `FLY_API_TOKEN` に export する（トークンには作成時に選ぶ有効期限がある。切れる前に更新すること）。

```bash
flyctl apps create <app>
flyctl volumes create bridge_data -r <region> -s 1 -a <app> --yes   # SQLite を /data に置く
flyctl secrets set -a <app> \
  CURSOR_WEBHOOK_URL="$CURSOR_WEBHOOK_URL" \
  CURSOR_WEBHOOK_API_KEY="$CURSOR_WEBHOOK_API_KEY" \
  MCP_API_KEY="$(openssl rand -hex 32)" \
  INBOUND_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
  ALLOWED_HOSTS=<app>.fly.dev \
  DB_PATH=/data/bridge.db
flyctl deploy --remote-only --ha=false -a <app>
```

- `MCP_API_KEY` と `INBOUND_WEBHOOK_SECRET` は Poke や Cursor から発行されるものでは**なく**、自分で生成する値。生成した `MCP_API_KEY` は手元に控えること（手順 3 で Poke に同じ値を入れる。Fly はシークレットの値を再表示しない）。
- `INBOUND_WEBHOOK_SECRET` は任意。未設定でも `ask_grokbot` は動くが、`POST /hooks/grokbot` が 503 になり、`bridge_status` の `inbound_webhook_secret_configured` が `false` になる。

### 3. Poke を接続する

Poke で MCP インテグレーションを追加し、次を入力する。

| 項目 | 値 |
|---|---|
| Name | 任意のラベル。例: `grokbot-mcp-bridge` |
| Server URL | `https://<app>.fly.dev/mcp`（Streamable HTTP）。クライアントが SSE しか扱えない場合は `https://<app>.fly.dev/sse` |
| API Key | `MCP_API_KEY` の値（`Bearer ` は付けない）。Poke では任意扱いだが、空にすると OAuth を試みて失敗するので必ず設定する |

### 4. 疎通を確認する

1. `GET https://<app>.fly.dev/healthz` → `200 {"ok":true}`
2. Poke から `bridge_status` を呼ぶ → `*_configured` がすべて `true`
3. Poke から `ask_grokbot` を `payload={"message": "短く自己紹介してください。"}`、`wait_seconds=60` で呼ぶ
   - `answer_status: "answered"` → `answer_text` に回答が入る
   - `answer_status: "pending"` → `wait_for_grokbot_answer(run_id, timeout_seconds=120)` を呼ぶ
4. `flyctl logs -a <app>` に `run created` → `callback resolved` → `trigger returning` が並ぶ

Grok Bot 側の契約（`run_id` のエコー、`callback_url` への POST）とトラブルシューティングは [poke-invocation.ja.md](poke-invocation.ja.md) を参照。

## エンドポイント

- `GET /healthz`
- `GET /`
- `POST /mcp`（認証付き MCP）
- `GET /sse` と `POST /messages/`（認証付き MCP）
- `POST /hooks/grokbot`（署名付きの Grok Bot 受信イベント）
- `POST /callbacks/{token}`（Grok Bot からの回答）

## MCP ツール

- `bridge_status`
- `ask_grokbot`
- `get_grokbot_run`
- `wait_for_grokbot_answer`
- `list_grokbot_runs`
- `list_grokbot_events`
- `get_grokbot_event`

リソースは `grokbot://events` と `grokbot://runs` で参照できます。

## 環境変数

`CURSOR_WEBHOOK_URL`, `CURSOR_WEBHOOK_API_KEY`, `MCP_API_KEY`,
`INBOUND_WEBHOOK_SECRET`, `DB_PATH`, `ALLOWED_HOSTS`, `PUBLIC_BASE_URL`,
`CALLBACK_TTL_SECONDS`, `CALLBACK_ALLOW_HTTP`, `CALLBACK_ALLOWED_HOSTS`

## コールバック契約

Grok Bot は次のような JSON を POST します。

```json
{"ok": true, "answer": "...", "run_id": "<エコーした UUID>"}
```

`run_id`（または `request_id`）には、`ask_grokbot` が送った UUID をそのまま返す必要があります。
受信 webhook にコールバック URL がない場合、ブリッジは `「コールバックURLなし」` を返し、
コールバックは送らずにイベントだけ記録します。

## コマンド

```bash
uv run --extra dev pytest -q               # テスト
uv lock && uv export --no-dev --format requirements-txt --no-emit-project -o requirements.txt   # 依存関係のピン留めを更新
flyctl deploy --remote-only --ha=false     # デプロイ（app / region は fly.toml）
```
