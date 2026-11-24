"""YouTube live detection that does not spend Data API quota on the poll.

Detection order, cheapest first:

1. HTTP GET of the public ``/live`` URL (0 quota units). A redirect to
   ``/watch?v=`` means the channel is live. The HTML ``og:title`` is kept
   when it is present.
2. If the title is still missing, oEmbed for that video (0 quota units),
   cached per video id for this process.
3. Only if both of those fail and ``YOUTUBE_API_KEY`` is set, one
   ``videos.list`` call (1 unit) per video id.

``search.list`` (100 units) is intentionally not used. Polling it every
45 seconds would burn the default 10,000 unit daily quota in under two hours.
"""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from cliphunter.config import ChannelConfig
from cliphunter.jsonlog import log_event
from cliphunter.platforms.base import LiveStatus, PlatformError

_VIDEO_IN_URL = re.compile(r"(?:watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})")
_CANONICAL = re.compile(
    r'rel="canonical"\s+href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"',
    re.IGNORECASE,
)
_VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_TITLE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:title["\']',
    re.IGNORECASE,
)
_USELESS_TITLES = {"", "youtube", "youtube live", "- youtube"}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def youtube_live_url(channel: ChannelConfig) -> str:
    if channel.url:
        url = channel.url.strip()
        if "/live" in url or "watch?" in url or "youtu.be/" in url:
            return url
        return url.rstrip("/") + "/live"
    handle = (channel.handle or "").strip()
    if "youtube.com" in handle or "youtu.be" in handle:
        if not handle.startswith("http"):
            handle = "https://" + handle.lstrip("/")
        return youtube_live_url(
            ChannelConfig(platform="youtube", url=handle, authorized=channel.authorized)
        )
    if handle.startswith("UC") and len(handle) >= 20 and " " not in handle:
        return f"https://www.youtube.com/channel/{handle}/live"
    if not handle.startswith("@"):
        handle = f"@{handle}"
    return f"https://www.youtube.com/{handle}/live"


def _html_says_live(page: str) -> bool:
    compact = page.replace(" ", "")
    if '"isLive":true' in compact or '"isLiveNow":true' in compact:
        return True
    if '"liveBroadcastContent":"live"' in compact:
        return True
    return '\\"isLive\\":true' in page


def _title_from_html(page: str) -> str | None:
    match = _OG_TITLE.search(page) or _OG_TITLE_REV.search(page)
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    if title.lower().endswith(" - youtube"):
        title = title[: -len(" - youtube")].strip()
    if title.lower() in _USELESS_TITLES:
        return None
    return title[:300]


def _video_id_from_html(page: str) -> str | None:
    canonical = _CANONICAL.search(page)
    if canonical:
        return canonical.group(1)
    found = _VIDEO_ID.search(page)
    return found.group(1) if found else None


def interpret_youtube_page(requested_url: str, final_url: str, page: str) -> LiveStatus:
    """Pure parser used by the client and by tests."""

    final_id_match = _VIDEO_IN_URL.search(final_url)
    final_id = final_id_match.group(1) if final_id_match else None
    title = _title_from_html(page)
    requested_live_tab = "/live" in requested_url
    landed_on_watch = final_id is not None and "/watch" in final_url
    if landed_on_watch and (requested_live_tab or _html_says_live(page)):
        return LiveStatus(
            is_live=True,
            stream_url=f"https://www.youtube.com/watch?v={final_id}",
            title=title,
            video_id=final_id,
        )
    if _html_says_live(page):
        video_id = final_id or _video_id_from_html(page)
        if video_id:
            return LiveStatus(
                is_live=True,
                stream_url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                video_id=video_id,
            )
    return LiveStatus(is_live=False, reason="offline")


class YouTubePlatform:
    name = "youtube"

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key or None
        self._client = client
        self._owns_client = client is None
        self.quota_units = 0
        self._titles: dict[str, str] = {}
        self._api_attempted: set[str] = set()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            )
        return self._client

    async def check_live(self, channel: ChannelConfig) -> LiveStatus:
        requested = youtube_live_url(channel)
        client = await self._http()
        try:
            response = await client.get(
                requested,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                    "Cookie": "CONSENT=YES+1",
                },
            )
        except httpx.HTTPError as exc:
            raise PlatformError(f"youtube request failed: {exc}") from exc
        final_url = str(response.url)
        if "consent.youtube.com" in final_url or "consent.google.com" in final_url:
            raise PlatformError("youtube consent wall")
        if response.status_code == 429 or response.status_code >= 500:
            raise PlatformError(f"youtube http {response.status_code}")
        if response.status_code >= 400:
            return LiveStatus(is_live=False, reason=f"http_{response.status_code}")
        status = interpret_youtube_page(requested, final_url, response.text)
        if not status.is_live or not status.video_id:
            return status
        title = status.title or self._titles.get(status.video_id)
        if title is None:
            title = await self._title_from_oembed(status.video_id)
        if title is None:
            title = await self._title_from_api(status.video_id)
        if title:
            self._titles[status.video_id] = title
        return LiveStatus(
            is_live=True,
            stream_url=status.stream_url,
            title=title,
            started_at=status.started_at,
            video_id=status.video_id,
        )

    async def _title_from_oembed(self, video_id: str) -> str | None:
        client = await self._http()
        watch = f"https://www.youtube.com/watch?v={video_id}"
        try:
            response = await client.get(
                "https://www.youtube.com/oembed",
                params={"url": watch, "format": "json"},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return None
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:300]
        return None

    async def _title_from_api(self, video_id: str) -> str | None:
        if not self.api_key or video_id in self._api_attempted:
            return None
        self._api_attempted.add(video_id)
        self.quota_units += 1
        log_event(
            "INFO",
            "youtube.quota",
            units=1,
            total_units=self.quota_units,
            endpoint="videos.list",
            video_id=video_id,
        )
        client = await self._http()
        try:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet", "id": video_id, "key": self.api_key},
            )
        except httpx.HTTPError as exc:
            log_event("WARNING", "youtube.api_failed", error=str(exc))
            return None
        if response.status_code != 200:
            log_event("WARNING", "youtube.api_failed", status=response.status_code)
            return None
        items = response.json().get("items") or []
        if not items:
            return None
        title = (items[0].get("snippet") or {}).get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:300]
        return None
