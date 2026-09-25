# AGENTS.md

## テスト

```bash
uv run --extra dev pytest -q
```

- pytest は `pyproject.toml` の `dev` extra にだけ定義されている。`.venv/bin/python -m pytest` や `python3 -m pytest` では pytest が無い／`.venv` の依存を使わないことがある。

## 命名

- ブリッジの名前は `grokbot-mcp-bridge` に統一する（`fly.toml` の既定 app 名、`GET /` の `service`、コールバック POST の `User-Agent`、パッケージ名、リポジトリ名も同じ）。別名は使わない。

## 汎用性

- ドキュメント・コードを特定の環境や bot に依存させない。個別の bot 名（例: Grok Bot 上の Bot 名）、ルーチン名や folder、アプリ内リンク（`grokbot://app/...`）、bot の役割やメンバー構成、回答言語、特定時点の稼働状況は書かない。
- デプロイ先ホストは `https://<app>.fly.dev` と書き、`<app>` は `fly.toml` の `app` とする。

## デプロイ

- `flyctl deploy --remote-only --ha=false`（app / region は `fly.toml`）。
- 初回はボリューム `bridge_data`（`/data` にマウント）の作成と、SPEC 7章の Fly secrets 設定が必要。`ALLOWED_HOSTS` / `PUBLIC_BASE_URL` はデプロイ先のホストに合わせる。

## ドキュメント

- `README` / `SPEC` / `poke-invocation` は英語版（`*.md`）と日本語版（`*.ja.md`）の対。片方を直したらもう片方も直す。
- 仕様の正は `SPEC` と `app/main.py`。`poke-invocation` はそれに合わせる。
- 秘密（Webhook key / Bearer / Fly secrets の値）は書かない。
