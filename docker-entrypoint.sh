#!/bin/sh
set -e
mkdir -p "$(dirname "${DB_PATH:-/data/bridge.db}")" && chown -R app:app "$(dirname "${DB_PATH:-/data/bridge.db}")" 2>/dev/null || true
exec setpriv --reuid=app --regid=app --init-groups "$@"
