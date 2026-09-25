## 概要
<!-- 何を・なぜ変更したか。読者は diff を見ていない前提で 2〜3 行 -->

## 変更の種類
- [ ] 実装（`app/`）
- [ ] ドキュメント（README / SPEC / poke-invocation / REVIEW / AGENTS）
- [ ] CI・デプロイ設定（`.github/` / `fly.toml` / `Dockerfile`）
- [ ] テスト

## 影響範囲
<!-- 影響する境界にチェック。該当なしなら「なし」と書く -->
- [ ] MCP 認証（`/mcp` `/sse` `/messages`、`MCP_API_KEY`）
- [ ] 送信 webhook（Grok Bot への POST、`run_id` / `request_id` / callback URL）
- [ ] コールバック（`/callbacks/{token}`、UUID 相関、回答正規化）
- [ ] 受信 webhook（`/hooks/grokbot`、HMAC / Bearer）
- [ ] SSRF ガード（コールバック URL 検証）
- [ ] 永続化（SQLite `/data/bridge.db`）
- [ ] 環境変数・Fly secrets の追加・変更（名前のみ記載、値は書かない）

## 確認事項
- [ ] `uv run --extra dev pytest -q` が全件通る
- [ ] 実装を変えた場合、SPEC.md / SPEC.ja.md を実装に合わせて更新した（実装が正）
- [ ] ドキュメントを変えた場合、日英の対（`*.md` / `*.ja.md`）を両方更新した
- [ ] app 名は `grokbot-mcp-bridge`、ホストは `https://<app>.fly.dev` のプレースホルダで書いた（特定環境の固定値・旧名を入れていない）
- [ ] シークレットの値（`crsr_…`、`MCP_API_KEY`、`INBOUND_WEBHOOK_SECRET` 等）を本文・コミット・ログ出力に含めていない
- [ ] デプロイが必要な場合、手順（`flyctl deploy --remote-only --ha=false`）と必要な secrets 変更を記載した

## 動作確認
<!-- 実行したコマンドと結果の要約。ask_grokbot を試した場合は run_id と answer_status（本文の秘密はマスク） -->

## 補足・レビューして欲しい点
<!-- 判断が分かれる点、既知の制限、フォローアップなど -->
