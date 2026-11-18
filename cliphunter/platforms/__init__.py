"""Build the platform registry from environment settings."""

from __future__ import annotations

import httpx

from cliphunter.env import EnvSettings
from cliphunter.platforms.base import Platform
from cliphunter.platforms.kick import KickPlatform
from cliphunter.platforms.twitch import TwitchPlatform
from cliphunter.platforms.youtube import YouTubePlatform

__all__ = ["EnvSettings", "Platform", "build_platforms"]


def build_platforms(
    env: EnvSettings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Platform]:
    return {
        "youtube": YouTubePlatform(api_key=env.youtube_api_key, client=client),
        "twitch": TwitchPlatform(
            client_id=env.twitch_client_id,
            client_secret=env.twitch_client_secret,
            client=client,
        ),
        "kick": KickPlatform(),
    }
