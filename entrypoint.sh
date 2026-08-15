#!/usr/bin/env bash
set -euo pipefail

# Single entrypoint, dispatched by the first arg. Migrations are their OWN
# command run by a dedicated one-shot `migrate` service in docker-compose, so
# that api and worker (which can each be scaled to N replicas) never run
# `alembic upgrade head` concurrently and race on creating the enum type.
case "${1:-api}" in
  migrate)
    echo "[entrypoint] applying migrations..."
    exec alembic upgrade head
    ;;
  api)
    echo "[entrypoint] starting API..."
    # --no-access-log: our own structured middleware emits one JSON line per
    # request, so uvicorn's plain-text access log would just be noisy duplicates.
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
    ;;
  worker)
    echo "[entrypoint] starting worker..."
    exec python -m app.worker
    ;;
  *)
    exec "$@"
    ;;
esac
