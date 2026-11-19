from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from mediafetch.config import Config
from mediafetch.downloader import supported_url, _platform
from mediafetch.main import handle_download, handle_watch, health
from mediafetch.store import Store, WebMedia
from mediafetch.ui import format_audio_caption, quality_keyboard
from mediafetch.downloader import Quality, MediaInfo


def test_supported_urls_new_platforms() -> None:
    urls = [
        "https://www.tiktok.com/@user/video/1234567890",
        "https://vm.tiktok.com/ZM8abcde/",
        "https://vt.tiktok.com/ZS8abcde/",
        "https://pinterest.com/pin/1234567890/",
        "https://www.pinterest.com/pin/1234567890/",
        "https://pin.it/7xyz",
        "https://facebook.com/watch/?v=123456",
        "https://www.facebook.com/reel/123456",
        "https://fb.watch/xyz123/",
        "https://reddit.com/r/funny/comments/abc1234/test_post/",
        "https://www.reddit.com/r/funny/comments/abc1234/test_post/",
        "https://redd.it/abc1234",
        "https://v.redd.it/xyz1234",
        "https://threads.net/@user/post/abc123",
        "https://www.threads.net/@user/post/abc123",
    ]
    for url in urls:
        assert supported_url(url) is True, f"Failed for {url}"


def test_platform_detection() -> None:
    assert _platform({"extractor": "tiktok"}) == "TikTok"
    assert _platform({"extractor": "pinterest"}) == "Pinterest"
    assert _platform({"extractor": "facebook"}) == "Facebook"
    assert _platform({"extractor": "reddit"}) == "Reddit"
    assert _platform({"extractor": "threads"}) == "Threads"


def test_quality_keyboard_includes_audio() -> None:
    kb = quality_keyboard("job123", [Quality(1080, 50000000), Quality(720, 25000000)])
    rows = kb["inline_keyboard"]
    # Check that audio button is present
    audio_btn = [btn for row in rows for btn in row if btn.get("callback_data") == "quality:job123:audio"]
    assert len(audio_btn) == 1
    assert "MP3" in audio_btn[0]["text"]


def test_format_audio_caption() -> None:
    info = MediaInfo(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Rick Astley - Never Gonna Give You Up",
        uploader="RickAstleyVEVO",
        duration=212,
        width=1920,
        height=1080,
        platform="YouTube",
        automatic_best=False,
        qualities=[],
        raw={},
        caption="Official music video for Rick Astley",
    )
    caption = format_audio_caption(info, 5000000)
    assert "Rick Astley - Never Gonna Give You Up" in caption
    assert "Audio Extracted" in caption
    assert "320kbps" in caption


def test_web_media_store(tmp_path: Path) -> None:
    db_file = tmp_path / "web_media_test.db"
    store = Store(db_file)

    test_file = tmp_path / "song.mp3"
    test_file.write_bytes(b"dummy audio content")

    store.add_web_media(
        "token_123",
        WebMedia(
            token="token_123",
            file_path=str(test_file),
            filename="song.mp3",
            title="A Cool Song",
            content_type="audio/mpeg",
            size=len(b"dummy audio content"),
            owner_id=999,
        ),
    )

    media = store.get_web_media("token_123")
    assert media is not None
    assert media.filename == "song.mp3"
    assert media.title == "A Cool Song"
    assert media.content_type == "audio/mpeg"

    # Test purging expired files
    # Manually backdate the created_at in database
    with store._connection:
        past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)).isoformat()
        store._connection.execute("UPDATE web_media SET created_at = ? WHERE token = 'token_123'", (past,))

    purged = store.purge_expired_web_media(max_age_hours=24)
    assert len(purged) == 1
    assert not test_file.exists()
    assert store.get_web_media("token_123") is None


@pytest.mark.asyncio
async def test_web_routes(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    db_file = tmp_path / "web_routes_test.db"
    store = Store(db_file)

    media_file = tmp_path / "test_video.mp4"
    media_file.write_bytes(b"dummy mp4 video payload")

    store.add_web_media(
        "tok_vid",
        WebMedia(
            token="tok_vid",
            file_path=str(media_file),
            filename="test_video.mp4",
            title="Awesome Video Title",
            content_type="video/mp4",
            size=len(b"dummy mp4 video payload"),
            owner_id=123,
        ),
    )

    app = web.Application()
    app["store"] = store
    app.router.add_get("/health", health)
    app.router.add_get("/dl/{token}", handle_download)
    app.router.add_get("/watch/{token}", handle_watch)

    async with TestClient(TestServer(app)) as client:
        # Health check
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

        # Download route
        resp = await client.get("/dl/tok_vid")
        assert resp.status == 200
        content = await resp.read()
        assert content == b"dummy mp4 video payload"
        assert 'attachment; filename="test_video.mp4"' in resp.headers.get("Content-Disposition", "")

        # Watch route
        resp = await client.get("/watch/tok_vid")
        assert resp.status == 200
        html = await resp.text()
        assert "Awesome Video Title" in html
        assert "<video" in html
        assert "/dl/tok_vid" in html

        # Missing token
        resp = await client.get("/dl/nonexistent")
        assert resp.status == 404
