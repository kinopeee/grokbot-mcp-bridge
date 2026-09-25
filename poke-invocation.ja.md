# Poke から Grok Bot を呼び出す — 仕様と手順

[English](poke-invocation.md) | 日本語

対象: Poke → MCP ブリッジ → Grok Bot Webhook →（コールバック）→ ブリッジ → Poke

---

## 1. 全体像

```
Poke
  │  MCP tools/call（答えを待つ）
  ▼
grokbot-mcp-bridge（Fly）
  │  HTTP POST（非同期起こし）
  ▼
Grok Bot の webhook トリガ付きルーチン
  │  ① Grok Bot のチャットに短く報告
  │  ② callback_url へ答えを POST
  ▼
ブリッジが待ち Run を UUID で解決
  │
  ▼
Poke に MCP 結果として返る
```

**重要制約:** Grok Bot の Webhook HTTP 応答ボディには答えを載せられない。  
Webhook の HTTP 200 は「受け付けた」だけの ack。答えの届け先は次の2つだけ。

1. Grok Bot のチャット（ユーザー向け報告）
2. ペイロードに明示されたコールバック URL への POST

Poke が「同じ HTTP レスポンスに答えが来る」前提だと、この経路では満たせない。ブリッジが非同期コールバックを待ち、Poke の MCP `tools/call` に転送する必要がある。

---

## 2. 登場要素

| 要素 | 役割 |
|------|------|
| **Poke** | ユーザーが話しかける先。ブリッジを MCP サーバとして呼ぶ |
| **ブリッジ** | `https://<app>.fly.dev`（`<app>` は `fly.toml` の `app`。既定は `grokbot-mcp-bridge`） |
| **Grok Bot Webhook** | Grok Bot のルーチン（自動実行）を起こす webhook トリガ |
| **Grok Bot** | Webhook 依頼に答え、コールバックする Bot。Bot 名・役割・口調は Grok Bot 側の設定に従い、ブリッジは関与しない |

### ブリッジの主なエンドポイント

| パス | 用途 |
|------|------|
| `GET /` | サービス案内（`mcp_streamable_http` / `mcp_sse` / `inbound_webhook`） |
| `GET /healthz` | ヘルスチェック |
| `GET /docs` / `GET /openapi.json` | OpenAPI |
| `POST/GET /mcp` | MCP Streamable HTTP（要 Bearer） |
| `/sse` | MCP SSE |
| `POST /hooks/grokbot` | ブリッジの inbound Grok Bot webhook |
| `POST /callbacks/{token}` | トークン付きコールバック |

---

## 3. 同期契約（相関 ID）

Poke／ブリッジが待つのは **UUID 形式の `run_id`（または `request_id`）**。

| やってよいこと | やってはいけないこと |
|----------------|----------------------|
| 着信ペイロードの UUID をそのままエコー | 新しい ID を自分で発行する |
| `run_id` / `request_id` の両方に同じ UUID を載せる | テスト用ラベル（例: `grok-test-...`）を相関に使う |
| 応答の数値 `run_id: 3` は「ブリッジ受付番号」と理解する | 数値受付番号で待ちを解決しようとすること |

優先して読むフィールド（UUID らしき値を最優先）:

1. `run_id` / `runId`
2. `request_id` / `requestId` / `id`
3. ヘッダ `X-Run-Id` / `X-Request-Id` など
4. UUID が無いときだけ文字列 ID にフォールバックし、チャットに「UUIDなし」と書く

成功時のコールバック応答例（ブリッジ）:

```json
{"ok":true,"run_id":"<着信と同じUUID>"}
```

既に回答済みの Run に再送すると `HTTP 409` / `already_answered` になることがある（想定内）。

---

## 4. Webhook 着信（ブリッジ → Grok Bot）

### Grok Bot 側のルーチン

- トリガ: `{ "type": "webhook" }`
- Webhook URL / key / Authorization ヘッダは Grok Bot アプリのルーチン設定から取得する（値はドキュメントに書かない）

### 沈黙してよい着信（ユーザーにもコールバックにも送らない）

- MCP `initialize` / notifications などの握手
- `test: true` / `smoke_test` / 空ボディ
- 意味のある依頼文がないもの

### 実依頼として扱う着信

`message` / `text` / `query` / `prompt` / `content` などから依頼文を取り出す。回答の範囲や内容は Grok Bot 側の設定に従う。

---

## 5. コールバック（Grok Bot → ブリッジ）

### コールバック URL の探し方（優先順）

ペイロードまたはヘッダの:

- `callback_url` / `reply_url` / `response_url` / `reply_to`
- `X-Poke-Callback-Url` など

推測の宛先には送らない。URL が無いときはチャットに「コールバックURLなし」と書く。

### POST ボディ

必須に近いフィールドを欠かさない（ブリッジ／Poke 両対応のためキーを複数載せる）:

```json
{
  "ok": true,
  "run_id": "<着信のUUIDそのまま>",
  "request_id": "<同上>",
  "answer": "<回答本文>",
  "message": "<回答本文>",
  "content": "<回答本文>",
  "text": "<回答本文>",
  "hasMore": false
}
```

失敗時:

```json
{
  "ok": false,
  "run_id": "<着信のUUIDそのまま>",
  "request_id": "<同上>",
  "error": "<理由>",
  "hasMore": false
}
```

### 認証

ペイロード／ヘッダに `callback_token` / `reply_token` / `X-Poke-Callback-Token` / `Authorization` があれば:

```
Authorization: Bearer <token>
Content-Type: application/json
```

無ければ Bearer なしで POST。

### チャット報告に必ず含める項目

- ホスト（例: `<app>.fly.dev`）
- エコーした `run_id`（UUID かどうか）
- HTTP ステータス
- 応答ボディ要約（数値受付番号と待ち UUID を混同しないこと）

---

## 6. 手順（セットアップ〜疎通）

### A. Grok Bot 側（済みなら確認のみ）

1. webhook トリガのルーチンが有効であること
2. ルーチン設定から Webhook URL / key / Authorization をコピー
3. ブリッジの secrets（`CURSOR_WEBHOOK_URL` / `CURSOR_WEBHOOK_API_KEY`）に入れる

### B. ブリッジ側

1. Poke から MCP として `https://<app>.fly.dev/mcp`（または `/sse`）を登録
2. `ask_grokbot` が呼び出しごとに UUID4 を発行し、`run_id` / `request_id` の両方に載せて Grok Bot Webhook へ POST する（呼び出し側の指定は上書き）
3. `callback_url` / `reply_url` / `response_url` は、呼び出し側が指定しなければブリッジが自動付与する（トークン付き `/callbacks/{token}`）
4. コールバック受信後、その UUID の待ちを解決し、MCP `tools/call` 結果として Poke に本文を返す（`wait_seconds` 内に届かなければ `pending` を返すので、`wait_for_grokbot_answer` で回収する）

Poke が実際に送るもの（`tools/call`）:

```json
{"name": "ask_grokbot", "arguments": {"payload": {"message": "短く自己紹介してください。"}, "wait_seconds": 60}}
```

`payload` は（`run_id` / `request_id` / コールバック URL を足したうえで）そのまま Grok Bot webhook に転送されるので、依頼文は `message` に入れる。`wait_seconds` は 0〜120 に丸められる。結果は JSON 文字列:

| `answer_status` | Poke がすること |
|---|---|
| `answered` | `answer_text` を表示する |
| `pending` | 返ってきた `run_id` で `wait_for_grokbot_answer` を呼ぶ（`timeout_seconds` は最大 120）。`summary` にもその旨が書かれる。`get_grokbot_run(run_id)` なら待たずに同じレコードを取れる |

`ask_grokbot` が `"error": "bridge_not_configured"` を返す場合はブリッジに `CURSOR_WEBHOOK_URL` / `CURSOR_WEBHOOK_API_KEY` が無い。まず `bridge_status` で確認する。

### C. 疎通テストの進め方

1. **握手・空テスト** → Grok Bot は沈黙（正常）
2. **実依頼**（例: 「自己紹介して」）を Poke から投げる
3. Grok Bot のチャットに短い回答＋コールバック報告が出る
4. コールバックが `HTTP 200` かつ同じ UUID をエコーしているか確認
5. Poke UI に答えが出るか確認

### D. 切り分け表

| 観測 | 推定箇所 |
|------|----------|
| Grok Bot のチャットにも何も出ない | Webhook 未到達／握手扱いで沈黙／ルーチン無効 |
| チャットには出るがコールバック失敗 | URL・認証・ボディ形 |
| コールバック 200・UUID 一致なのに Poke に出ない | **ブリッジ → Poke**（ログ・MCP 転送／表示） |
| コールバック 409 `already_answered` | その UUID は既回答。新しい Run で再試行 |
| 応答が `run_id: 3` のような数値だけ | 受付番号。待ち UUID とは別物 |

ブリッジ運用側で確認する3点:

1. その `run_id` のコールバックはブリッジログに残っているか
2. 残っているなら MCP `tools/call` 結果を Poke に返したか
3. Poke が結果の `answer_text` を表示しているか（`pending` なら `summary` の指示どおり `wait_for_grokbot_answer` で回収する）

### E. 詰まりやすい区間

ブリッジ → Poke への MCP 転送／表示。とくに `wait_seconds` を過ぎて `pending` になった Run を、Poke が `wait_for_grokbot_answer` で回収しないと答えが表示されない。

---

## 7. 運用メモ

- 外向き送信（メール・Slack など）は、ペイロードが明示していない限りしない
- 秘密（Webhook key / Bearer / MCP_API_KEY）はドキュメントに書かない。値は Grok Bot アプリの設定と Fly secrets で管理する

---

## 8. 関連リンク

- ブリッジ: `https://<app>.fly.dev/`
- ブリッジ OpenAPI: `https://<app>.fly.dev/openapi.json`
- ブリッジの仕様: [SPEC.ja.md](SPEC.ja.md)
