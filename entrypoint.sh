#!/usr/bin/env bash
set -euo pipefail

# Wait for Postgres, then apply migrations before starting any process. Running
# `alembic upgrade head` on every boot is idempotent and keeps a clean clone
# working with a single `docker compose up`.
echo "[entrypoint] applying migrations..."
alembic upgrade head

case "${1:-api}" in
  api)
    echo "[entrypoint] starting API..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    echo "[entrypoint] starting worker..."
    # app.worker is added in Stage 3.
    exec python -m app.worker
    ;;
  *)
    exec "$@"
    ;;
esac
