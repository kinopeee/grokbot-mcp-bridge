# Poke から Eric（Grok Bot）を呼び出す — 仕様と手順

[English](poke-invocation.md) | 日本語

最終更新: 2026-09-26（Asia/Tokyo）  
対象: Poke → MCP ブリッジ → Grok Bot Webhook →（コールバック）→ ブリッジ → Poke

---

## 1. 全体像

```
Poke
  │  MCP tools/call（答えを待つ）
  ▼
cursor-mcp-bridge（Fly）
  │  HTTP POST（非同期起こし）
  ▼
Grok Bot ルーチン「Web webhook」
  │  ① このチャットに日本語で短く報告
  │  ② callback_url へ答えを POST
  ▼
ブリッジが待ち Run を UUID で解決
  │
  ▼
Poke に MCP 結果として返る（※ここが未完了のことがある）
```

**重要制約:** Grok Bot の Webhook HTTP 応答ボディには答えを載せられない。  
Webhook の HTTP 200 は「受け付けた」だけの ack。答えの届け先は次の2つだけ。

1. この Eric チャット（ユーザー向け報告）
2. ペイロードに明示されたコールバック URL への POST

Poke が「同じ HTTP レスポンスに答えが来る」前提だと、この経路では満たせない。ブリッジが非同期コールバックを待ち、Poke の MCP `tools/call` に転送する必要がある。

---

## 2. 登場要素

| 要素 | 役割 |
|------|------|
| **Poke** | ユーザーが話しかける先。ブリッジを MCP サーバとして呼ぶ |
| **ブリッジ** | `https://cursor-mcp-bridge-kinopee.fly.dev`（サービス名 `cursor-mcp-bridge`） |
| **Grok Bot Webhook** | ルーチン「Web webhook」（folder: `web-webhook`）を起こす入口 |
| **Eric** | プロジェクト運営 Bot。専門作業はしない。Webhook 依頼に答え、コールバックする |

### ブリッジの主なエンドポイント

| パス | 用途 |
|------|------|
| `GET /` | サービス案内（`mcp_streamable_http` / `mcp_sse` / `inbound_webhook`） |
| `GET /healthz` | ヘルスチェック |
| `GET /docs` / `GET /openapi.json` | OpenAPI |
| `POST/GET /mcp` | MCP Streamable HTTP（要 Bearer） |
| `/sse` | MCP SSE |
| `POST /hooks/grokbot` | ブリッジの inbound Grok Bot webhook |
| `POST /callbacks` | 相関 UUID だけで答えを受け取る |
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

### ルーチン

- 名前: **Web webhook**
- folder: `web-webhook`
- トリガ: `{ "type": "webhook" }`
- 設定リンク（アプリ内）:
  - [Webhook URL](grokbot://app/v1/sidebar?target=webhook-url&automation=web-webhook)
  - [Webhook key](grokbot://app/v1/sidebar?target=webhook-key&automation=web-webhook)
  - [Authorization header](grokbot://app/v1/sidebar?target=webhook-header&automation=web-webhook)

### 沈黙してよい着信（ユーザーにもコールバックにも送らない）

- MCP `initialize` / notifications などの握手
- `test: true` / `smoke_test` / 空ボディ
- 意味のある依頼文がないもの

### 実依頼として扱う着信

`message` / `text` / `query` / `prompt` / `content` などから依頼文を取り出す。

Eric の役割範囲:

- プロジェクト運営・スタッフ配置・未割当 / Blocked の確認
- 専門作業そのものはしない（必要なら Eng などへ振る方針を返す）

---

## 5. コールバック（Grok Bot → ブリッジ）

### コールバック URL の探し方（優先順）

ペイロードまたはヘッダの:

- `callback_url` / `reply_url` / `response_url` / `reply_to`
- `X-Poke-Callback-Url` など

推測の宛先には送らない。URL が無いときはチャットに「コールバックURLなし」と書く。

### POST ボディ（現状の送り方）

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

- ホスト（例: `cursor-mcp-bridge-kinopee.fly.dev`）
- エコーした `run_id`（UUID かどうか）
- HTTP ステータス
- 応答ボディ要約（数値受付番号と待ち UUID を混同しないこと）

---

## 6. 手順（セットアップ〜疎通）

### A. Grok Bot 側（済みなら確認のみ）

1. ルーチン「Web webhook」が有効であること
2. [Webhook URL](grokbot://app/v1/sidebar?target=webhook-url&automation=web-webhook) / key / Authorization をコピー
3. ブリッジ設定にその URL（と必要なら key / Bearer）を入れる

### B. ブリッジ側

1. Poke から MCP として `https://cursor-mcp-bridge-kinopee.fly.dev/mcp`（または `/sse`）を登録
2. ツール呼び出し時、Grok Bot Webhook へ POST するペイロードに **待ちと同じ UUID** を `run_id`（推奨）で載せる
3. `callback_url`（またはトークン付き `/callbacks/{token}`）を載せる
4. コールバック受信後、その UUID の待ちを解決し、MCP `tools/call` 結果として Poke に本文を返す

### C. 疎通テストの進め方

1. **握手・空テスト** → Eric は沈黙（正常）
2. **実依頼**（例: 「君の名は？」）を Poke から投げる
3. Eric チャットに短い日本語回答＋コールバック報告が出る
4. コールバックが `HTTP 200` かつ同じ UUID をエコーしているか確認
5. Poke UI に答えが出るか確認

### D. 切り分け表

| 観測 | 推定箇所 |
|------|----------|
| Eric チャットにも何も出ない | Webhook 未到達／握手扱いで沈黙／ルーチン無効 |
| チャットには出るがコールバック失敗 | URL・認証・ボディ形 |
| コールバック 200・UUID 一致なのに Poke に出ない | **ブリッジ → Poke**（ログ・MCP 転送・読むキー） |
| コールバック 409 `already_answered` | その UUID は既回答。新しい Run で再試行 |
| 応答が `run_id: 3` のような数値だけ | 受付番号。待ち UUID とは別物 |

Poke／Devin 側に確認してもらうとよい3点:

1. その `run_id` のコールバックはブリッジログに残っているか
2. 残っているなら MCP `tools/call` 結果を Poke に返したか
3. 答え本文はどの JSON キーを読むか（いまは `answer` / `message` / `content` / `text` を全部送っている）

---

## 7. 現状（2026-09-26 時点）

| 区間 | 状態 |
|------|------|
| Poke → ブリッジ → Grok Bot Webhook | 実依頼は到達している |
| Grok Bot → チャット報告 | 動いている |
| Grok Bot → ブリッジ コールバック（UUID エコー・HTTP 200） | 動いている |
| ブリッジ → Poke UI への表示 | **未解決のことが多い**（転送 or 読むキー） |

---

## 8. 運用メモ

- 外向き送信（メール・Slack など）は、ペイロードが明示していない限りしない
- 新規専門 Bot の作成は、既存で足りないときだけユーザー承認後
- 開発メンバー例: Eng Mgr、Eng 1〜5、（プラグイン／API ラップなら）tinkabot
- このドキュメントは運用メモ。秘密（Webhook key / Bearer）は書かない。値はアプリのサイドバーリンクから参照する

---

## 9. 関連リンク

- ルーチン Webhook URL: [Webhook URL](grokbot://app/v1/sidebar?target=webhook-url&automation=web-webhook)
- ルーチン Webhook key: [Webhook key](grokbot://app/v1/sidebar?target=webhook-key&automation=web-webhook)
- Authorization: [Authorization header](grokbot://app/v1/sidebar?target=webhook-header&automation=web-webhook)
- ブリッジ: `https://cursor-mcp-bridge-kinopee.fly.dev/`
- ブリッジ OpenAPI: `https://cursor-mcp-bridge-kinopee.fly.dev/openapi.json`
