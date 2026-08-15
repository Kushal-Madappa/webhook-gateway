"""Structured JSON logging, shared by the API and the worker.

One JSON object per line -> greppable by humans and parseable by log pipelines
(Loki/ELK/Datadog) without a regex. Any keyword passed via `logger.info(msg,
extra={...})` is merged into the object, so domain context (event_id, status,
attempts) rides alongside the message instead of being embedded in a string.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Standard LogRecord attributes we do NOT want to echo as "extra" fields.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge any structured extras (event_id, status, ...).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # httpx logs one INFO line per delivery; that's our own "delivered" log's
    # job, so keep the library quiet to avoid duplicate noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
