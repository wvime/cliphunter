import httpx

from cliphunter.config import ChannelConfig
from cliphunter.platforms.base import LiveStatus
from cliphunter.platforms.kick import KickPlatform
from cliphunter.platforms.twitch import TwitchPlatform, twitch_login
from cliphunter.platforms.youtube import YouTubePlatform, interpret_youtube_page, youtube_live_url


def _response(url: str, *, status: int = 200, text: str = "", body: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", url)
    if body is not None:
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, text=text, request=request)


class _Router:
    def __init__(self, routes: list[tuple[str, httpx.Response]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        self.calls.append(str(url))
        for needle, response in self.routes:
            if needle in str(url):
                return response
        raise AssertionError(f"unexpected URL {url}")


def test_youtube_live_url_from_handle_and_channel():
    handle = ChannelConfig(platform="youtube", handle="@Ibai")
    assert youtube_live_url(handle) == "https://www.youtube.com/@Ibai/live"
    bare = ChannelConfig(platform="youtube", handle="Ibai")
    assert youtube_live_url(bare) == "https://www.youtube.com/@Ibai/live"
    page = ChannelConfig(platform="youtube", url="https://www.youtube.com/@Ibai")
    assert youtube_live_url(page) == "https://www.youtube.com/@Ibai/live"


def test_interpret_redirect_to_watch_is_live_and_offline_page_is_not():
    live = interpret_youtube_page(
        "https://www.youtube.com/@Ibai/live",
        "https://www.youtube.com/watch?v=abcdefghijk",
        '<meta property="og:title" content="Directo de prueba - YouTube">',
    )
    assert live.is_live
    assert live.video_id == "abcdefghijk"
    assert live.title == "Directo de prueba"
    assert live.stream_url == "https://www.youtube.com/watch?v=abcdefghijk"
    offline = interpret_youtube_page(
        "https://www.youtube.com/@Ibai/live",
        "https://www.youtube.com/@Ibai/live",
        "<html><title>Ibai - YouTube</title></html>",
    )
    assert not offline.is_live


async def test_youtube_check_does_not_spend_quota_when_title_is_free():
    final = "https://www.youtube.com/watch?v=abcdefghijk"
    page = _response(
        final,
        text='<meta property="og:title" content="Ranked">',
    )
    client = _Router([("youtube.com", page)])
    platform = YouTubePlatform(api_key="should-not-be-used", client=client)  # type: ignore[arg-type]
    status = await platform.check_live(ChannelConfig(platform="youtube", handle="@Ibai"))
    assert status.is_live
    assert status.title == "Ranked"
    assert platform.quota_units == 0
    assert len(client.calls) == 1
    assert all("googleapis" not in call for call in client.calls)


async def test_youtube_api_is_last_resort_and_cached():
    final = "https://www.youtube.com/watch?v=abcdefghijk"
    page = _response(final, text="<html></html>")
    missing = _response("https://www.youtube.com/oembed", status=404, text="no")
    api = _response(
        "https://www.googleapis.com/youtube/v3/videos",
        body={"items": [{"snippet": {"title": "API Title"}}]},
    )
    client = _Router(
        [
            ("oembed", missing),
            ("googleapis", api),
            ("youtube.com", page),
        ]
    )
    platform = YouTubePlatform(api_key="test-key", client=client)  # type: ignore[arg-type]
    channel = ChannelConfig(platform="youtube", handle="@Ibai")
    first = await platform.check_live(channel)
    second = await platform.check_live(channel)
    assert first.title == "API Title"
    assert second.title == "API Title"
    assert platform.quota_units == 1
    assert sum("googleapis" in call for call in client.calls) == 1


def test_twitch_login_and_kick_stub():
    login = twitch_login(ChannelConfig(platform="twitch", url="https://www.twitch.tv/Gaules"))
    assert login == "gaules"
    assert KickPlatform.implemented is False


async def test_kick_is_explicitly_not_implemented():
    status = await KickPlatform().check_live(ChannelConfig(platform="kick", handle="someone"))
    assert status == LiveStatus(is_live=False, reason="kick_not_implemented", definitive=True)


async def test_twitch_missing_credentials_does_not_call_out():
    status = await TwitchPlatform().check_live(ChannelConfig(platform="twitch", handle="gaules"))
    assert status.is_live is False
    assert status.reason == "missing_credentials"


class _TwitchHttp:
    def __init__(self) -> None:
        self.posts = 0
        self.gets = 0

    async def post(self, url: str, **_kwargs: object) -> httpx.Response:
        self.posts += 1
        return httpx.Response(
            200,
            json={"access_token": "tok", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.gets += 1
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"
        assert headers["Client-ID"] == "id"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "9",
                        "user_login": "gaules",
                        "title": "Ranked",
                        "started_at": "2026-01-01T00:00:00Z",
                    }
                ]
            },
            request=httpx.Request("GET", url),
        )


async def test_twitch_helix_marks_live_and_caches_token():
    http = _TwitchHttp()
    platform = TwitchPlatform("id", "secret", client=http)  # type: ignore[arg-type]
    channel = ChannelConfig(platform="twitch", handle="Gaules")
    first = await platform.check_live(channel)
    second = await platform.check_live(channel)
    assert first.is_live and second.is_live
    assert first.stream_url == "https://www.twitch.tv/gaules"
    assert first.title == "Ranked"
    assert first.video_id == "9"
    assert http.posts == 1
    assert http.gets == 2
