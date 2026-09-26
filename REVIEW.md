# REVIEW.md

Devin Review 向けのレビュー方針。このリポジトリは Poke（MCP クライアント）と Grok Bot（Cursor automation webhook）を仲介する単一プロセスの FastAPI ブリッジで、実質的なコードは `app/main.py` 1 ファイル。外部から到達可能な認証境界が 3 つあるため、変更はまず「どの境界に触れているか」で見る。

## 最重要（セキュリティ境界）

`app/main.py` の次の箇所への変更は必ず精査する。

- **MCP 認証** `require_mcp_auth` / `_PROTECTED_PREFIXES`（`/mcp` `/sse` `/messages`）
  - `MCP_API_KEY` 未設定時は 503、キー不一致は 401 を返し続けること。保護対象パスが減っていないか、`startswith` の境界（`/mcp` と `/mcpx` など）が壊れていないか。
- **受信 webhook 認証** `inbound_grokbot_webhook` / `_verify_signature`
  - 受け付けるのは `X-Webhook-Signature: sha256=<HMAC-SHA256(body, INBOUND_WEBHOOK_SECRET)>` と `Authorization: Bearer <INBOUND_WEBHOOK_SECRET>` の 2 方式。ヘッダー名を変える変更は `SPEC.md` / `SPEC.ja.md` §6 と `tests/test_bridge.py` を同時に更新していなければ指摘する。
  - 署名は**生のリクエストボディ**に対して計算する。パース後の JSON を再シリアライズして検証する変更は不可。
- **コールバック解決** `_resolve_callback` / `/callbacks/{token}`
  - `token` と本文の `run_id`（または `request_id`）の両方で照合し、不一致は 400。
  - `already_answered`（409）/ `callback_expired`（410, `CALLBACK_TTL_SECONDS`）の判定と、`UPDATE ... WHERE status = 'pending'` による二重回答防止を弱めていないか。
  - `_read_limited_body` の `MAX_CALLBACK_BODY`（256 KiB）チェックは Content-Length の事前チェックと、ボディのストリーム読み途中の中断で行う。`request.body()` で全量をメモリにためる実装への回帰は chunked transfer-encoding で DoS になるため不可。
- **SSRF ガード** `_is_safe_callback_url` / `_host_matches`
  - 受信 webhook 本文の `callback_url`（`reply_url` / `response_url`）へ POST する前に必ず通す。`CALLBACK_ALLOWED_HOSTS` 未設定は全拒否（フェイルクローズ）、許可リスト外ホスト、https 以外、userinfo 付き、80/443 以外のポート、自ホスト（`ALLOWED_HOSTS`）、`localhost` / `.internal` / `.local`、private / loopback / link-local および `is_global` でないアドレス（`100.64.0.0/10` 等）は拒否。拒否理由文字列を減らす・順序を変える変更は挙動差分を確認する。
  - `CALLBACK_ALLOW_HTTP=1` はテスト専用の緩和で、http スキームの許可のみ（許可リスト・IP/ポート検査は常に有効）。本番向けコードパスやドキュメントで既定化していないか。
- **秘密比較** はすべて `hmac.compare_digest` を使う。`==` での比較や、キーの一部をログ・レスポンス・例外メッセージに出す変更は不可。

## 汎用性・命名（AGENTS.md の規約を強制する）

- ブリッジ名は `grokbot-mcp-bridge` に統一。`fly.toml` の `app`、`GET /` の `service`、コールバック POST の `User-Agent`、パッケージ名のどれかだけを変える変更は指摘する。
- 特定の環境・bot に依存する記述（個別 bot 名、ルーチン名や folder、`grokbot://app/...` などのアプリ内リンク、bot の役割やメンバー構成、回答言語、特定時点の稼働状況、実際の Fly app ホスト名）をコード・ドキュメントに持ち込まない。ホストは `https://<app>.fly.dev` と書く。
- `.env.example` にはキー名だけ。値の例であっても実物に見える文字列（`crsr_...` など）は入れない。

## ドキュメントの整合

- `README` / `SPEC` / `poke-invocation` は英語版（`*.md`）と日本語版（`*.ja.md`）が対。片方だけ更新された PR は指摘する。
- 仕様の正は `SPEC.md` と `app/main.py`。両者が食い違っている場合は実装を正とし、`SPEC` 側の修正を求める（実装を SPEC に合わせるなら PR 説明に明記されていること）。
- MCP ツール（`bridge_status` `ask_grokbot` `get_grokbot_run` `wait_for_grokbot_answer` `list_grokbot_runs` `list_grokbot_events` `get_grokbot_event`）のシグネチャや返却 JSON のキー（`run_id` `answer_status` `answer_text` など）を変えたら、`SPEC` §4 と `poke-invocation` の両言語版も更新されているか確認する。
- `bridge_status` は「設定済みか」の真偽値だけを返す。秘密の値や部分文字列を返す変更は不可。

## テスト

- テストコマンドは `uv run --extra dev pytest -q` のみ（CI もこれを実行）。`python -m pytest` など別コマンド前提の変更は指摘する。
- `tests/test_bridge.py` は先頭で環境変数を固定してから `app.main` を import している。モジュール読み込み時に評価される設定（`ALLOWED_HOSTS` `PUBLIC_BASE_URL` `CALLBACK_TTL_SECONDS` など）を追加・変更する場合、テスト側の初期化も追従しているか。
- 認証・署名・SSRF・コールバック相関に関わる変更は、対応するテスト（`test_bad_signature` `test_callback_url_safety` `test_overlapping_grokbot_runs_require_matching_uuid` `test_concurrent_grokbot_callbacks_are_correlated` など）の追加・更新なしでは承認しない。
- 検証を通すためにテストの期待値を緩める変更は不可。

## 永続化・並行性

- SQLite は `DB_PATH`（本番は Fly volume 上の `/data/bridge.db`）に WAL モードで置く。`runs` / `events` のスキーマ変更は既存 DB との互換（`CREATE TABLE IF NOT EXISTS` の追記で済むか、マイグレーションが要るか）を確認する。
- `_db()` は都度接続・都度 close。接続をグローバルに保持する、`finally` で close しない変更は指摘する。
- `ask_grokbot` の待機は `_answer_waiters`（`asyncio.Event`）で行う。イベントの登録・解除漏れや、`wait_seconds` / `timeout_seconds` 上限の撤廃はリクエストの張り付きにつながる。
- ログには `run_id` / `status` などの相関情報のみ。`payload` 全文や回答本文、ヘッダー値をログに出す変更は指摘する。`httpx` / `httpcore` ロガーは WARNING 以上に抑え（INFO だと `CURSOR_WEBHOOK_URL` 全体が出る）、`uvicorn.access` には `/callbacks/<token>` をマスクするフィルタを付ける。これらを外す変更は指摘する。

## デプロイ

- `fly.toml`: `app = "grokbot-mcp-bridge"`、`primary_region`、`[mounts]`（`bridge_data` → `/data`）、`internal_port = 8080` の変更は理由が PR 説明にあること。
- `Dockerfile` / `requirements.txt` / `uv.lock` / `pyproject.toml` の依存変更は同期していること。`mcp` パッケージのメジャー更新は Streamable HTTP / SSE のマウント方法が変わる可能性があるため `app/main.py` の該当箇所を確認する。
- デプロイ手順の記述は `flyctl deploy --remote-only --ha=false` を維持する。

## レビュー対象外

- `uv.lock` は依存の追加・更新を伴わない差分ならスキップ可。
- `.github/workflows/ci.yml` は `uv run --extra dev pytest -q` を実行し続けていれば細部は問わない。
