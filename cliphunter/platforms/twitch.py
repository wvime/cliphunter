"""Twitch Helix live check. Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET."""

from __future__ import annotations

import re
import time

import httpx

from cliphunter.config import ChannelConfig
from cliphunter.platforms.base import LiveStatus, PlatformError

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_STREAMS_URL = "https://api.twitch.tv/helix/streams"
_LOGIN = re.compile(r"twitch\.tv/([A-Za-z0-9_]+)", re.IGNORECASE)
_RESERVED = {"videos", "directory", "settings", "subscriptions", "popout"}


def twitch_login(channel: ChannelConfig) -> str:
    if channel.handle:
        login = channel.handle.strip().lstrip("@").split("/")[-1]
        if login:
            return login.lower()
    if channel.url:
        match = _LOGIN.search(channel.url)
        if match and match.group(1).lower() not in _RESERVED:
            return match.group(1).lower()
    raise PlatformError("no pude obtener el login de Twitch (handle o url)")


class TwitchPlatform:
    name = "twitch"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id or None
        self.client_secret = client_secret or None
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_deadline = 0.0

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        return self._client

    async def check_live(self, channel: ChannelConfig) -> LiveStatus:
        if not self.client_id or not self.client_secret:
            return LiveStatus(is_live=False, reason="missing_credentials", definitive=True)
        login = twitch_login(channel)
        payload = await self._fetch_stream(login, refresh=False)
        if payload is None:
            payload = await self._fetch_stream(login, refresh=True)
        if payload is None:
            raise PlatformError("twitch helix rejected the token")
        data = payload.get("data") or []
        if not data:
            return LiveStatus(is_live=False, reason="offline")
        stream = data[0]
        user = stream.get("user_login") or login
        return LiveStatus(
            is_live=True,
            stream_url=f"https://www.twitch.tv/{user}",
            title=(stream.get("title") or None),
            started_at=stream.get("started_at"),
            video_id=str(stream.get("id")) if stream.get("id") is not None else None,
        )

    async def _fetch_stream(self, login: str, *, refresh: bool) -> dict | None:
        token = await self._app_token(force=refresh)
        client = await self._http()
        try:
            response = await client.get(
                _STREAMS_URL,
                params={"user_login": login},
                headers={
                    "Client-ID": self.client_id or "",
                    "Authorization": f"Bearer {token}",
                },
            )
        except httpx.HTTPError as exc:
            raise PlatformError(f"twitch request failed: {exc}") from exc
        if response.status_code == 401:
            self._token = None
            return None
        if response.status_code == 429 or response.status_code >= 500:
            raise PlatformError(f"twitch http {response.status_code}")
        if response.status_code >= 400:
            raise PlatformError(f"twitch http {response.status_code}")
        return response.json()

    async def _app_token(self, *, force: bool) -> str:
        now = time.monotonic()
        if self._token and not force and now < self._token_deadline:
            return self._token
        client = await self._http()
        try:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.HTTPError as exc:
            raise PlatformError(f"twitch token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise PlatformError(f"twitch token http {response.status_code}")
        body = response.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise PlatformError("twitch token response had no access_token")
        expires = float(body.get("expires_in") or 3600)
        self._token = token
        self._token_deadline = time.monotonic() + max(30.0, expires - 60.0)
        return token
