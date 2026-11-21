from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import yt_dlp

from .ui import Quality


logger = logging.getLogger(__name__)


def _patch_parth_dl() -> None:
    try:
        import parth_dl.extractors

        orig_graphql = parth_dl.extractors.MediaExtractor._parse_graphql_media
        orig_item = parth_dl.extractors.MediaExtractor._parse_media_item

        def _patched_graphql(self, media):
            res = orig_graphql(self, media)
            edges = media.get("edge_media_to_caption", {}).get("edges") or []
            if edges:
                text = edges[0].get("node", {}).get("text", "")
                if text:
                    res["caption"] = text
                    res["title"] = text
            return res

        def _patched_item(self, item):
            res = orig_item(self, item)
            cap = item.get("caption") or {}
            if isinstance(cap, dict):
                text = cap.get("text", "")
                if text:
                    res["caption"] = text
                    res["title"] = text
            return res

        parth_dl.extractors.MediaExtractor._parse_graphql_media = _patched_graphql
        parth_dl.extractors.MediaExtractor._parse_media_item = _patched_item
    except Exception as exc:
        logger.warning("Could not patch parth_dl extractor: %s", exc)


_patch_parth_dl()


SUPPORTED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
    "www.instagr.am",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "m.tiktok.com",
    "pinterest.com",
    "www.pinterest.com",
    "pin.it",
    "in.pinterest.com",
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "fb.watch",
    "web.facebook.com",
    "reddit.com",
    "www.reddit.com",
    "redd.it",
    "v.redd.it",
    "old.reddit.com",
    "threads.net",
    "www.threads.net",
}


class MediaError(RuntimeError):
    pass


def _extractor_args() -> dict[str, dict[str, list[str]]]:
    provider_url = os.getenv("YTDLP_POT_PROVIDER_URL", "http://127.0.0.1:4416").strip()
    args: dict[str, dict[str, list[str]]] = {
        "youtube": {
            "player_client": ["mweb", "web", "android"],
        }
    }
    if provider_url:
        args["youtubepot-bgutilhttp"] = {"base_url": [provider_url.rstrip("/")]}
    return args


def get_cookie_file() -> str | None:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    candidates = [
        data_dir / "cookies.txt",
        Path("cookies.txt"),
        Path("/etc/secrets/cookies.txt"),
    ]
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return str(p.resolve())
        except Exception:
            pass
    return None


_cookie_file = get_cookie_file


@dataclass(frozen=True)
class MediaInfo:
    url: str
    title: str
    uploader: str
    duration: int
    width: int | None
    height: int | None
    platform: str
    automatic_best: bool
    qualities: list[Quality]
    raw: dict[str, Any]
    caption: str = ""


def supported_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme == "https" and (parsed.hostname or "").lower() in SUPPORTED_HOSTS
    )


def _estimate_by_height(formats: list[dict[str, Any]], height: int) -> int | None:
    video_sizes = [
        int(item.get("filesize") or item.get("filesize_approx") or 0)
        for item in formats
        if item.get("height") == height and item.get("vcodec") != "none"
    ]
    audio_sizes = [
        int(item.get("filesize") or item.get("filesize_approx") or 0)
        for item in formats
        if item.get("vcodec") == "none" and item.get("acodec") != "none"
    ]
    video = max(video_sizes, default=0)
    audio = max(audio_sizes, default=0)
    return video + audio if video else None


def quality_options(info: dict[str, Any]) -> list[Quality]:
    formats = info.get("formats") or []
    available = {
        int(item["height"])
        for item in formats
        if item.get("height") and item.get("vcodec") != "none"
    }
    standard = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    selected = [h for h in standard if h in available]
    if not selected and available:
        selected = sorted(available, reverse=True)[:6]
    return [
        Quality(height, _estimate_by_height(formats, height))
        for height in selected
    ]


def _platform(info: dict[str, Any]) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if "youtube" in extractor:
        return "YouTube"
    if "instagram" in extractor:
        return "Instagram"
    if "twitter" in extractor or extractor == "x":
        return "X"
    if "tiktok" in extractor:
        return "TikTok"
    if "pinterest" in extractor:
        return "Pinterest"
    if "facebook" in extractor:
        return "Facebook"
    if "reddit" in extractor:
        return "Reddit"
    if "threads" in extractor:
        return "Threads"
    return extractor.title() or "Media"


def _automatic_best(url: str, info: dict[str, Any], platform: str) -> bool:
    if platform != "YouTube":
        return True
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    duration = int(info.get("duration") or 0)
    return "/shorts/" in url.lower() or (height > width > 0 and 0 < duration <= 180)


def _source_has_audio(info: dict[str, Any]) -> bool:
    if info.get("acodec") not in {None, "none"}:
        return True
    return any(
        item.get("acodec") not in {None, "none"} for item in (info.get("formats") or [])
    )


def _audio_is_required(info: MediaInfo) -> bool:
    if info.platform == "Instagram":
        entries = info.raw.get("entries") or []
        if any(item.get("kind") == "image" for item in entries) or info.raw.get("type") in {
            "post",
            "carousel",
            "image",
        }:
            return False
        video_entries = [item for item in entries if item.get("kind") == "video"]
        if video_entries:
            return any(
                any(fmt.get("has_audio") for fmt in entry.get("formats", []))
                for entry in video_entries
            )
    if _source_has_audio(info.raw):
        return True
    if info.platform != "Instagram":
        return False
    formats = info.raw.get("formats") or []
    return not formats or any(item.get("acodec") is None for item in formats)


def _format_selector(height: int | None, *, combined_only: bool = False) -> str:
    limit = f"[height<={height}]" if height else ""
    if combined_only:
        return f"b{limit}[vcodec!=none][acodec!=none]"
    return f"bv{limit}+ba/b{limit}"


def _audio_stream(path: Path) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                os.getenv("FFPROBE_BIN", "ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,profile,channels,sample_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError("Audio verification could not run") from exc
    if result.returncode != 0:
        raise MediaError("Could not read the downloaded media streams")
    try:
        streams = json.loads(result.stdout).get("streams") or []
    except (ValueError, AttributeError) as exc:
        raise MediaError("Invalid audio verification result") from exc
    return streams[0] if streams else None


def _audio_codec(path: Path) -> str | None:
    stream = _audio_stream(path)
    return stream.get("codec_name") if stream else None


def _telegram_audio_compatible(stream: dict[str, Any] | None) -> bool:
    return bool(
        stream
        and stream.get("codec_name") == "aac"
        and stream.get("profile") == "LC"
        and 1 <= int(stream.get("channels") or 0) <= 2
    )


def _has_audio_stream(path: Path) -> bool:
    return _audio_codec(path) is not None


def _instagram_candidate_formats(info: MediaInfo, height: int | None) -> list[str]:
    if info.platform != "Instagram":
        return []
    formats = [
        item
        for item in (info.raw.get("formats") or [])
        if item.get("format_id")
        and item.get("url")
        and item.get("vcodec") != "none"
        and (not height or not item.get("height") or int(item["height"]) <= height)
    ]
    formats.sort(
        key=lambda item: (
            int(item.get("height") or 0),
            int(item.get("width") or 0),
            float(item.get("fps") or 0),
            float(item.get("tbr") or 0),
            int(item.get("filesize") or item.get("filesize_approx") or 0),
        ),
        reverse=True,
    )
    return list(dict.fromkeys(str(item["format_id"]) for item in formats))[:10]


def _normalize_audio_for_telegram(path: Path) -> Path:
    stream = _audio_stream(path)
    if stream is None:
        raise MediaError("Downloaded video has no audio track")
    if _telegram_audio_compatible(stream) and path.suffix.lower() == ".mp4":
        return path

    logger.info(
        "Converting audio codec=%s profile=%s channels=%s to AAC-LC stereo; video=copy",
        stream.get("codec_name"),
        stream.get("profile"),
        stream.get("channels"),
    )

    converted = path.with_name(f"{path.stem}.telegram.mp4")
    result = _run_audio_conversion(
        [
            os.getenv("FFMPEG_BIN", "ffmpeg"),
            "-y",
            "-nostdin",
            "-threads",
            "4",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-sn",
            "-dn",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-disposition:a:0",
            "default",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(converted),
        ]
    )
    if result.returncode != 0 or not converted.exists():
        converted.unlink(missing_ok=True)
        raise MediaError("Could not convert the source audio for Telegram")

    if not _telegram_audio_compatible(_audio_stream(converted)):
        converted.unlink(missing_ok=True)
        raise MediaError("Telegram-compatible audio validation failed")
    final_path = path.with_suffix(".mp4")
    converted.replace(final_path)
    if final_path != path:
        path.unlink(missing_ok=True)
    return final_path


def _run_audio_conversion(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, check=False, text=True, timeout=1200
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError("Audio conversion could not finish") from exc


def _download_selectors(info: MediaInfo, height: int | None) -> list[str]:
    selectors = [
        _format_selector(height),
        _format_selector(height, combined_only=True),
        *_instagram_candidate_formats(info, height),
    ]
    return list(dict.fromkeys(selectors))


def _instagram_public_media_urls(info: MediaInfo, height: int | None) -> list[str]:
    if info.platform != "Instagram":
        return []
    try:
        from parth_dl import get_info

        public_info = get_info(info.url, verbose=False)
    except Exception as exc:
        logger.warning(
            "Anonymous Instagram fallback unavailable: %s", type(exc).__name__
        )
        return []

    formats = [
        item
        for entry in (public_info.get("entries") or [])
        if entry.get("kind") == "video"
        for item in (entry.get("formats") or [])
        if item.get("url")
        and (not height or not item.get("height") or int(item["height"]) <= height)
    ]
    formats.sort(
        key=lambda item: (
            bool(item.get("has_audio")),
            int(item.get("height") or 0),
            int(item.get("width") or 0),
        ),
        reverse=True,
    )
    return list(dict.fromkeys(str(item["url"]) for item in formats))[:10]


def _download_attempts(info: MediaInfo, height: int | None):
    for selector in _download_selectors(info, height):
        yield info.url, selector, "native"
    for media_url in _instagram_public_media_urls(info, height):
        yield media_url, "best", "public-embed"


def _media_files(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _clear_job_files(directory: Path) -> None:
    for path in directory.iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def probe_sync(url: str) -> MediaInfo:
    if not supported_url(url):
        raise MediaError("Unsupported link")

    parsed = urlparse(url.strip())
    is_ig = (parsed.hostname or "").lower() in {
        "instagram.com",
        "www.instagram.com",
        "instagr.am",
        "www.instagr.am",
    }
    if is_ig:
        try:
            from parth_dl import get_info

            ig_info = get_info(url, verbose=False)
            if ig_info and (ig_info.get("entries") or ig_info.get("id")):
                entries = ig_info.get("entries") or []
                caption = str(ig_info.get("caption") or ig_info.get("title") or "").strip()
                title = str(ig_info.get("title") or "Instagram media")
                uploader = str(ig_info.get("uploader") or "")
                duration = int(ig_info.get("duration") or 0)
                width = None
                height = None
                if entries and entries[0].get("formats"):
                    fmt0 = entries[0]["formats"][0]
                    width = int(fmt0["width"]) if fmt0.get("width") else None
                    height = int(fmt0["height"]) if fmt0.get("height") else None
                return MediaInfo(
                    url=url,
                    title=title,
                    uploader=uploader,
                    duration=duration,
                    width=width,
                    height=height,
                    platform="Instagram",
                    automatic_best=True,
                    qualities=[],
                    raw=ig_info,
                    caption=caption,
                )
        except Exception as exc:
            logger.info("parth_dl Instagram probe failed, falling back to yt-dlp: %s", exc)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cookiefile": _cookie_file(),
        "cookiesfrombrowser": None,
        "extractor_args": _extractor_args(),
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise MediaError(str(exc)) from exc
    except Exception as exc:
        raise MediaError(str(exc)) from exc
    if not info:
        raise MediaError("No media found")
    platform = _platform(info)
    title = str(info.get("title") or "Untitled video")
    description = str(info.get("description") or "").strip()
    caption = description if description else title
    return MediaInfo(
        url=url,
        title=title,
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        duration=int(info.get("duration") or 0),
        width=int(info["width"]) if info.get("width") else None,
        height=int(info["height"]) if info.get("height") else None,
        platform=platform,
        automatic_best=_automatic_best(url, info, platform),
        qualities=quality_options(info),
        raw=info,
        caption=caption,
    )


async def probe(url: str) -> MediaInfo:
    return await asyncio.to_thread(probe_sync, url)


ProgressCallback = Callable[[int, int | None, float | None], Awaitable[None]]


def _download_instagram(
    info: MediaInfo,
    directory: Path,
    max_bytes: int,
    cancel: threading.Event,
) -> list[Path]:
    if cancel.is_set():
        raise MediaError("Download cancelled")
    try:
        from parth_dl import InstagramDownloader

        dl = InstagramDownloader(verbose=False, overwrite=True)
        res = dl.download(
            info.url,
            output_path=directory,
            output_mode="directory",
            quality="best",
            info=info.raw if "entries" in info.raw else None,
        )
    except Exception as exc:
        logger.warning("parth_dl download failed: %s", exc)
        return []

    if cancel.is_set():
        raise MediaError("Download cancelled")

    if isinstance(res, (str, Path)):
        raw_paths = [Path(res)]
    elif isinstance(res, list):
        raw_paths = [Path(p) for p in res]
    else:
        raw_paths = _media_files(directory)

    valid_paths: list[Path] = []
    audio_required = _audio_is_required(info)

    for p in raw_paths:
        if not p.exists() or p.stat().st_size == 0:
            continue
        if p.stat().st_size > max_bytes:
            raise MediaError("Selected quality exceeds the configured size limit")

        if p.suffix.lower() == ".mp4":
            has_audio = _has_audio_stream(p)
            if audio_required and not has_audio:
                logger.warning("Instagram video has no audio track when required: %s", p)
                continue
            if has_audio:
                try:
                    p = _normalize_audio_for_telegram(p)
                except Exception as exc:
                    logger.warning("Audio normalization failed: %s", exc)
                    if audio_required:
                        raise
        valid_paths.append(p)

    return valid_paths


def _download_sync(
    info: MediaInfo,
    height: int | None,
    directory: Path,
    max_bytes: int,
    cancel: threading.Event,
    loop: asyncio.AbstractEventLoop,
    progress: ProgressCallback,
) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    if info.platform == "Instagram":
        ig_paths = _download_instagram(info, directory, max_bytes, cancel)
        if ig_paths:
            return ig_paths

    last_update = 0.0

    def hook(status: dict[str, Any]) -> None:
        nonlocal last_update
        if cancel.is_set():
            raise yt_dlp.utils.DownloadError("Download cancelled")
        downloaded = int(status.get("downloaded_bytes") or 0)
        if downloaded > max_bytes:
            raise yt_dlp.utils.DownloadError(
                "Selected quality exceeds the configured size limit"
            )
        now = time.monotonic()
        if status.get("status") == "downloading" and now - last_update >= 2:
            last_update = now
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            speed = status.get("speed")
            asyncio.run_coroutine_threadsafe(
                progress(downloaded, int(total) if total else None, speed), loop
            )

    base_options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cookiefile": _cookie_file(),
        "cookiesfrombrowser": None,
        "extractor_args": _extractor_args(),
        "merge_output_format": "mp4",
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 2,
        "postprocessor_args": {"ffmpeg": ["-threads", "4"]},
    }
    audio_required = _audio_is_required(info)
    output: Path | None = None
    last_download_error: yt_dlp.utils.DownloadError | None = None
    for attempt, (target_url, selector, source) in enumerate(
        _download_attempts(info, height)
    ):
        if cancel.is_set():
            raise MediaError("Download cancelled")
        if attempt:
            _clear_job_files(directory)
        options = {
            **base_options,
            "format": selector,
        }
        try:
            logger.info(
                "Download attempt platform=%s source=%s format=%s",
                info.platform,
                source,
                selector,
            )
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(target_url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            if cancel.is_set() or "configured size limit" in str(exc):
                raise MediaError(str(exc)) from exc
            last_download_error = exc
            continue
        files = _media_files(directory)
        if not files:
            continue
        candidate = files[0]
        candidate_audio = _audio_stream(candidate)
        if audio_required and candidate_audio is None:
            logger.warning(
                "Rejected silent rendition platform=%s source=%s format=%s",
                info.platform,
                source,
                selector,
            )
            continue
        output = (
            _normalize_audio_for_telegram(candidate) if candidate_audio else candidate
        )
        if output:
            logger.info(
                "Selected rendition platform=%s source=%s format=%s",
                info.platform,
                source,
                selector,
            )
            break
    if output is None:
        if last_download_error and not audio_required:
            raise MediaError(str(last_download_error)) from last_download_error
        raise MediaError("Instagram did not expose an audio rendition for this Reel")
    if output.stat().st_size > max_bytes:
        raise MediaError("Selected quality exceeds the configured size limit")
    return [output]


async def download(
    info: MediaInfo,
    height: int | None,
    directory: Path,
    max_bytes: int,
    cancel: threading.Event,
    progress: ProgressCallback,
) -> list[Path]:
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        _download_sync, info, height, directory, max_bytes, cancel, loop, progress
    )


def _download_audio_sync(
    info: MediaInfo,
    directory: Path,
    cancel: threading.Event,
    loop: asyncio.AbstractEventLoop,
    progress: ProgressCallback,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    last_update = 0.0

    def hook(status: dict[str, Any]) -> None:
        nonlocal last_update
        if cancel.is_set():
            raise yt_dlp.utils.DownloadError("Download cancelled")
        now = time.monotonic()
        if status.get("status") == "downloading" and now - last_update >= 2:
            last_update = now
            downloaded = int(status.get("downloaded_bytes") or 0)
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            speed = status.get("speed")
            asyncio.run_coroutine_threadsafe(
                progress(downloaded, int(total) if total else None, speed), loop
            )

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cookiefile": _cookie_file(),
        "extractor_args": _extractor_args(),
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "postprocessor_args": {"ffmpeg": ["-threads", "4"]},
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(info.url, download=True)

    files = sorted(
        [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        files = sorted(
            [
                p
                for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".aac", ".opus"}
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    if not files:
        raise MediaError("Could not extract audio track")
    return files[0]


async def download_audio(
    info: MediaInfo,
    directory: Path,
    cancel: threading.Event,
    progress: ProgressCallback,
) -> Path:
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        _download_audio_sync, info, directory, cancel, loop, progress
    )


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def safe(value: str) -> str:
    return html.escape(value, quote=False)
