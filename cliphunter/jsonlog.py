"""One JSON object per line on stdout. Secret-looking fields are redacted."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from cliphunter.timeutil import utc_iso

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_level = "INFO"
_SECRET = re.compile(r"key|secret|token|authorization|password", re.IGNORECASE)
_QUERY_SECRET = re.compile(r"([?&](?:key|token|secret|client_secret)=)[^&\s]+", re.IGNORECASE)


def set_level(level: str) -> None:
    global _level
    normalized = level.upper()
    if normalized not in _LEVELS:
        raise ValueError(f"unknown log level {level}")
    _level = normalized


def current_level() -> str:
    return _level


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _QUERY_SECRET.sub(r"\1***", value)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET.search(key):
                cleaned[key] = "***"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def log_event(level: str, event: str, **fields: Any) -> None:
    normalized = level.upper()
    if _LEVELS[normalized] < _LEVELS[_level]:
        return
    payload = {"ts": utc_iso(), "level": normalized, "event": event, **redact(fields)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
