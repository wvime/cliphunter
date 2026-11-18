"""Process environment. Secrets stay in the environment, never in the repo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class EnvSettings:
    youtube_api_key: str | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None


def _clean(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_env(path: Path | None = None) -> EnvSettings:
    """Load ``.env`` if it exists. Existing process variables win."""

    if path is not None:
        if path.exists():
            load_dotenv(path, override=False)
    else:
        default = Path(".env")
        if default.exists():
            load_dotenv(default, override=False)
    return EnvSettings(
        youtube_api_key=_clean("YOUTUBE_API_KEY"),
        twitch_client_id=_clean("TWITCH_CLIENT_ID"),
        twitch_client_secret=_clean("TWITCH_CLIENT_SECRET"),
        anthropic_api_key=_clean("ANTHROPIC_API_KEY"),
        telegram_bot_token=_clean("TELEGRAM_BOT_TOKEN"),
    )
