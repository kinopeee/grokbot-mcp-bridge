---
name: deploy-fly
description: grokbot-mcp-bridge を Fly.io にデプロイする手順（main の最新をデプロイ、初回セットアップ、デプロイ後の確認、ロールバック）。「デプロイして」「最新コードを本番に反映して」と言われたときに使う。
---

# grokbot-mcp-bridge を Fly.io にデプロイする

## 前提

- `flyctl`（`fly`）が入っていること。無ければ `curl -L https://fly.io/install.sh | sh` で `~/.fly/bin` に入る（PATH に追加）。
- 認証は環境変数 `FLY_API_TOKEN`。flyctl はこれを自動で読むので `flyctl auth login` は不要。
  値は表示・ファイル保存しない（`flyctl auth whoami` で有効性だけ確認できる）。
- app 名・region・ボリューム・VM サイズは `fly.toml` が正（app は `grokbot-mcp-bridge`、region `nrt`、`/data` に `bridge_data`）。
  コマンドに `-a` を付ける場合も `fly.toml` の `app` と一致させる。
- デプロイは `--ha=false` 固定。レート制限や waiter がメモリ上の単一プロセス前提のため、マシンを複数にしない。

## 手順（通常デプロイ）

1. リポジトリのルートで、デプロイ対象を `main` の最新に合わせる。

```bash
cd <repo-root>
git status --porcelain        # 空であること（ローカル差分・未追跡ファイルもイメージに入るため）
git checkout main && git pull --ff-only
git log --oneline -1          # これがデプロイされるコミット
```

   `git status --porcelain` に出力があるときはデプロイしない（stash するか別の clean な checkout を使う）。

2. テストを通す（CI 済みでも、ローカル差分を含まないことの確認になる）。

```bash
uv run --extra dev pytest -q
```

3. デプロイ。ビルドは Fly のリモートビルダー（Depot）で行うのでローカルに Docker は不要。
   通常 1〜3 分。バックグラウンド化された場合は出力を待ち続け、`✔ Machine ... is now in a good state` と
   `Visit your newly deployed app at https://<app>.fly.dev/` が出るまで完了とみなさない。

```bash
flyctl deploy --remote-only --ha=false
```

4. 確認。

```bash
flyctl releases -a <app> | head -3                 # 先頭が新しい version で STATUS=complete
flyctl status -a <app>                             # マシンが 1 台 started
curl -sS https://<app>.fly.dev/                    # {"service":"grokbot-mcp-bridge",...} が返る
flyctl logs -a <app> --no-tail | tail -20          # 起動エラーが無いこと
```

   `/mcp` も必ず確認する（`MCP_API_KEY` 未設定だと `GET /` は 200 でも `/mcp` は 503 になる）。
   Bearer キー（Fly secrets の `MCP_API_KEY` の値。環境変数名は環境により異なる）付きで `tools/list` を叩き、
   `result.tools` にツール一覧が返ることを確認する:

```bash
curl -sS https://<app>.fly.dev/mcp \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## 注意

- `flyctl deploy` はイメージの push が終わった時点で以降の中断が効かない（マシン更新が始まる）。
  中止したい場合は「Building image」中に止める。ロールバックは次項。
- コードを変えていないのに再デプロイしても害は無い（同じイメージで新 release が作られるだけ）。
- Fly secrets（`CURSOR_WEBHOOK_URL`, `CURSOR_WEBHOOK_API_KEY`, `MCP_API_KEY`, `ALLOWED_HOSTS` など、一覧は `SPEC.ja.md` 7 章）は
  デプロイでは変わらない。変更が必要なときだけ `flyctl secrets set -a <app> KEY=value`（設定時にマシンが再起動する）。
  値はチャットやファイルに書かない。
- `fly.toml` を変更する PR をデプロイするときは、差分（VM サイズ・mounts・http_service）を先に読んで意図どおりか確認する。

## ロールバック

```bash
flyctl releases -a <app> --image | head -5        # 戻したい version の image ref を確認
flyctl deploy -a <app> --image <前の image ref> --ha=false
```

`--image` はコードだけを戻し、設定はカレントの `fly.toml` が使われる。戻したい release 以降に `fly.toml`
（VM サイズ・mounts・http_service など）が変わっている場合は、先に `git checkout <前のコミット> -- fly.toml`
で当時の設定に戻してから上記を実行する（Fly secrets はデプロイでは変わらないので別途対応）。

## 初回セットアップ（app が無いときだけ）

```bash
flyctl apps create <app>
flyctl volumes create bridge_data -r nrt -s 1 -a <app> --yes
# 必須。MCP_API_KEY はここで新規生成する（固定値・例示値をそのまま使わない）。生成した値は Poke 等のクライアント側にも設定する
flyctl secrets set -a <app> \
  CURSOR_WEBHOOK_URL=<Cursor automation の webhook URL> \
  CURSOR_WEBHOOK_API_KEY=<Cursor automation の crsr_ キー> \
  MCP_API_KEY="$(openssl rand -hex 32)" \
  ALLOWED_HOSTS=<app>.fly.dev
# 任意。受信 webhook（/hooks/grokbot）から callback_url 付きで呼ぶ運用のときだけ。ask_grokbot だけなら不要
flyctl secrets set -a <app> CALLBACK_ALLOWED_HOSTS=<許可するコールバック先ホスト>
flyctl deploy --remote-only --ha=false
```

`ALLOWED_HOSTS` / `PUBLIC_BASE_URL` はデプロイ先ホストに合わせる。詳細は `SPEC.ja.md` 7〜8 章と `AGENTS.md`。
