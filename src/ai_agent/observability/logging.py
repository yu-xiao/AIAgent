"""Structured JSON logging with defensive secret redaction."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SECRET = re.compile(
    r"(?i)(authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|cookie)"
    r"([\s\"'=:\\]+)([^\s,;\"}]+)"
)


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        for field in ("trace_id", "run_id"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = str(value)
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def _redact(value: str) -> str:
    return _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
