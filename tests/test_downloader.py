from pathlib import Path
from types import SimpleNamespace

import pytest

from mediafetch.downloader import (
    MediaError,
    MediaInfo,
    _audio_is_required,
    _format_selector,
    _has_audio_stream,
    _instagram_candidate_formats,
    _instagram_public_media_urls,
    _normalize_audio_for_telegram,
    _source_has_audio,
    quality_options,
    supported_url,
)


def test_supported_urls() -> None:
    assert supported_url("https://youtu.be/abc")
    assert supported_url("https://www.instagram.com/reel/abc/")
    assert supported_url("https://x.com/example/status/1")
    assert not supported_url("http://youtube.com/watch?v=abc")
    assert not supported_url("https://example.com/video")


def test_quality_options_are_standard_and_estimated() -> None:
    info = {
        "formats": [
            {
                "height": 1080,
                "vcodec": "avc1",
                "acodec": "none",
                "filesize": 20_000_000,
            },
            {"height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 10_000_000},
            {"height": None, "vcodec": "none", "acodec": "aac", "filesize": 2_000_000},
            {"height": 540, "vcodec": "avc1", "acodec": "aac", "filesize": 8_000_000},
        ]
    }
    qualities = quality_options(info)
    assert [item.height for item in qualities] == [1080, 720]
    assert qualities[0].estimated_bytes == 22_000_000


def test_format_selector_requires_audio_with_combined_fallback() -> None:
    assert _format_selector(None) == "bv+ba/b"
    assert _format_selector(2160) == "bv[height<=2160]+ba/b[height<=2160]"
    assert _format_selector(None, combined_only=True) == "b[vcodec!=none][acodec!=none]"


def test_source_audio_detection() -> None:
    assert _source_has_audio({"formats": [{"vcodec": "h264", "acodec": "aac"}]})
    assert _source_has_audio({"formats": [{"vcodec": "none", "acodec": "opus"}]})
    assert not _source_has_audio({"formats": [{"vcodec": "h264", "acodec": "none"}]})


def test_audio_stream_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "mediafetch.downloader.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_name":"aac","profile":"HE-AAC","channels":2}]}',
        ),
    )
    assert _has_audio_stream(Path("video.mp4"))


def _info(platform: str, formats: list[dict]) -> MediaInfo:
    return MediaInfo(
        url="https://www.instagram.com/reel/example/",
        title="Example",
        uploader="",
        duration=10,
        width=1080,
        height=1920,
        platform=platform,
        automatic_best=True,
        qualities=[],
        raw={"formats": formats},
    )


def test_instagram_unknown_audio_is_required() -> None:
    assert _audio_is_required(_info("Instagram", [{"acodec": None}]))
    assert not _audio_is_required(_info("Instagram", [{"acodec": "none"}]))
    assert not _audio_is_required(_info("X", [{"acodec": None}]))


def test_instagram_candidates_are_highest_first_and_height_limited() -> None:
    info = _info(
        "Instagram",
        [
            {"format_id": "low", "url": "https://cdn/low", "height": 720, "width": 405},
            {
                "format_id": "high",
                "url": "https://cdn/high",
                "height": 1080,
                "width": 608,
            },
            {"format_id": "audio", "url": "https://cdn/audio", "vcodec": "none"},
        ],
    )
    assert _instagram_candidate_formats(info, None) == ["high", "low"]
    assert _instagram_candidate_formats(info, 720) == ["low"]


def test_instagram_public_fallback_prefers_audio_and_quality(monkeypatch) -> None:
    monkeypatch.setattr(
        "parth_dl.get_info",
        lambda *args, **kwargs: {
            "entries": [
                {
                    "kind": "video",
                    "formats": [
                        {
                            "url": "https://cdn/silent",
                            "height": 1080,
                            "has_audio": False,
                        },
                        {"url": "https://cdn/audio", "height": 720, "has_audio": True},
                    ],
                }
            ]
        },
    )
    assert _instagram_public_media_urls(_info("Instagram", []), None) == [
        "https://cdn/audio",
        "https://cdn/silent",
    ]


@pytest.mark.parametrize(
    "codec,profile,extension",
    [
        ("opus", None, ".webm"),
        ("aac", "HE-AAC", ".mp4"),
        ("aac", "HE-AACv2", ".mp4"),
        ("aac", None, ".mp4"),
    ],
)
def test_incompatible_audio_is_converted(
    monkeypatch, tmp_path, codec, profile, extension
) -> None:
    source = tmp_path / f"clip{extension}"
    source.write_bytes(b"source")
    streams = iter(
        [
            {"codec_name": codec, "profile": profile, "channels": 2},
            {"codec_name": "aac", "profile": "LC", "channels": 2},
        ]
    )
    monkeypatch.setattr(
        "mediafetch.downloader._audio_stream", lambda path: next(streams)
    )

    def fake_run(args, **kwargs):
        assert args[args.index("-c:v") + 1] == "copy"
        assert args[args.index("-profile:a") + 1] == "aac_low"
        assert args[args.index("-ac") + 1] == "2"
        Path(args[-1]).write_bytes(b"converted")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("mediafetch.downloader.subprocess.run", fake_run)
    output = _normalize_audio_for_telegram(source)
    assert output == tmp_path / "clip.mp4"
    assert output.read_bytes() == b"converted"
    if source != output:
        assert not source.exists()


def test_aac_lc_stereo_is_kept_without_loss(monkeypatch, tmp_path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"original")
    monkeypatch.setattr(
        "mediafetch.downloader._audio_stream",
        lambda path: {
            "codec_name": "aac",
            "profile": "LC",
            "channels": 2,
        },
    )

    def unexpected_conversion(*args, **kwargs):
        pytest.fail("Already compatible audio should not be converted")

    monkeypatch.setattr("mediafetch.downloader.subprocess.run", unexpected_conversion)
    assert _normalize_audio_for_telegram(source).read_bytes() == b"original"


def test_failed_conversion_keeps_original(monkeypatch, tmp_path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"original")
    monkeypatch.setattr(
        "mediafetch.downloader._audio_stream",
        lambda path: {
            "codec_name": "aac",
            "profile": "HE-AAC",
            "channels": 2,
        },
    )
    monkeypatch.setattr(
        "mediafetch.downloader.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="error"
        ),
    )
    with pytest.raises(MediaError, match="convert"):
        _normalize_audio_for_telegram(source)
    assert source.read_bytes() == b"original"


def test_instagram_image_post_audio_not_required() -> None:
    # Single image post
    info_image = MediaInfo(
        url="https://www.instagram.com/p/testphoto/",
        title="Photo Post",
        uploader="photographer",
        duration=0,
        width=1080,
        height=1350,
        platform="Instagram",
        automatic_best=True,
        qualities=[],
        raw={"type": "post", "entries": [{"kind": "image", "formats": [{"url": "http://img"}]}]},
    )
    assert not _audio_is_required(info_image)

    # Carousel with mixed or image items
    info_carousel = MediaInfo(
        url="https://www.instagram.com/p/testcarousel/",
        title="Carousel Post",
        uploader="creator",
        duration=0,
        width=1080,
        height=1080,
        platform="Instagram",
        automatic_best=True,
        qualities=[],
        raw={"type": "carousel", "entries": [{"kind": "image"}]},
    )
    assert not _audio_is_required(info_carousel)


def test_instagram_probe_sync(monkeypatch) -> None:
    from mediafetch.downloader import probe_sync

    monkeypatch.setattr(
        "parth_dl.get_info",
        lambda url, verbose=False: {
            "id": "123456",
            "title": "Nice Sunset",
            "uploader": "nature",
            "duration": 0,
            "type": "post",
            "entries": [
                {
                    "kind": "image",
                    "formats": [{"url": "https://cdn.example.com/sunset.jpg", "width": 1080, "height": 1350}],
                }
            ],
        },
    )
    info = probe_sync("https://www.instagram.com/p/123456/")
    assert info.platform == "Instagram"
    assert info.title == "Nice Sunset"
    assert info.uploader == "nature"
    assert info.automatic_best is True
    assert info.width == 1080
    assert info.height == 1350


def test_instagram_download_carousel(monkeypatch, tmp_path) -> None:
    import threading
    from mediafetch.downloader import _download_instagram

    img1 = tmp_path / "img1.jpg"
    img2 = tmp_path / "img2.jpg"
    img1.write_bytes(b"image1_data")
    img2.write_bytes(b"image2_data")

    class DummyDownloader:
        def __init__(self, *args, **kwargs):
            pass

        def download(self, *args, **kwargs):
            return [str(img1), str(img2)]

    monkeypatch.setattr("parth_dl.InstagramDownloader", DummyDownloader)

    info = MediaInfo(
        url="https://www.instagram.com/p/carousel/",
        title="Album",
        uploader="artist",
        duration=0,
        width=1080,
        height=1080,
        platform="Instagram",
        automatic_best=True,
        qualities=[],
        raw={"type": "carousel", "entries": [{"kind": "image"}, {"kind": "image"}]},
    )

    results = _download_instagram(info, tmp_path, 10_000_000, threading.Event())
    assert len(results) == 2
    assert results[0] == img1
    assert results[1] == img2

