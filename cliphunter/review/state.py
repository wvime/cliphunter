"""Local Telegram chat id. Not a secret, but it is machine state, not repo content."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cliphunter.atomic import atomic_write_json
from cliphunter.config import AppConfig


@dataclass
class TelegramState:
    chat_id: int | None = None
    username: str | None = None
    offset: int | None = None

    def to_dict(self) -> dict:
        return {"chat_id": self.chat_id, "username": self.username, "offset": self.offset}

    @classmethod
    def from_dict(cls, payload: dict) -> TelegramState:
        raw_id = payload.get("chat_id")
        raw_offset = payload.get("offset")
        return cls(
            chat_id=int(raw_id) if raw_id is not None else None,
            username=payload.get("username"),
            offset=int(raw_offset) if raw_offset is not None else None,
        )


def state_path(config: AppConfig) -> Path:
    return config.resolved_database_path().parent / "telegram_chat.json"


def load_state(path: Path) -> TelegramState:
    if not path.exists():
        return TelegramState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TelegramState()
    if not isinstance(payload, dict):
        return TelegramState()
    return TelegramState.from_dict(payload)


def save_state(path: Path, state: TelegramState) -> None:
    atomic_write_json(path, state.to_dict())
