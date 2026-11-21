from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from .bot import Bot
from .config import Config
from .store import Store
from .telegram import Telegram
from .ui import human_bytes


logger = logging.getLogger(__name__)

HTML_PLAYER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} - MediaFetch</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f172a;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .container {{
      width: 100%;
      max-width: 800px;
      background: #1e293b;
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5), 0 8px 10px -6px rgba(0, 0, 0, 0.5);
      border: 1px solid #334155;
    }}
    .media-wrapper {{
      width: 100%;
      background: #000;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    video, audio {{
      width: 100%;
      max-height: 70vh;
      outline: none;
    }}
    audio {{
      padding: 2rem 1rem;
    }}
    .content {{
      padding: 1.5rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.25rem 0.6rem;
      background: #0284c7;
      color: #fff;
      font-size: 0.75rem;
      font-weight: 600;
      border-radius: 9999px;
      margin-bottom: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    h1 {{
      font-size: 1.25rem;
      font-weight: 600;
      line-height: 1.4;
      margin-bottom: 1rem;
      color: #f1f5f9;
      word-break: break-word;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
      color: #94a3b8;
      font-size: 0.875rem;
      margin-bottom: 1.5rem;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      padding: 0.75rem 1.5rem;
      font-size: 0.95rem;
      font-weight: 600;
      text-decoration: none;
      border-radius: 10px;
      transition: all 0.2s ease;
      cursor: pointer;
    }}
    .btn-primary {{
      background: #2563eb;
      color: #fff;
    }}
    .btn-primary:hover {{
      background: #1d4ed8;
    }}
    .footer {{
      margin-top: 1.5rem;
      color: #64748b;
      font-size: 0.8rem;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="media-wrapper">
      {player_tag}
    </div>
    <div class="content">
      <span class="badge">{badge_text}</span>
      <h1>{title}</h1>
      <div class="meta">
        <span>📦 Size: {size_text}</span>
        <span>⚡ Singapore High-Speed CDN</span>
      </div>
      <div class="actions">
        <a href="{download_url}" class="btn btn-primary" download>⬇️ Direct Download ({size_text})</a>
      </div>
    </div>
  </div>
  <div class="footer">
    MediaFetch High Performance Cloud &bull; Singapore Region
  </div>
</body>
</html>
"""


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def diag(request: web.Request) -> web.Response:
    url = request.query.get("url", "https://youtu.be/YihPV4eSHsQ")
    import yt_dlp
    from .downloader import get_cookie_file, _extractor_args

    cf = get_cookie_file()
    pot_active = False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:4416/ping", timeout=aiohttp.ClientTimeout(total=2)) as r:
                pot_active = r.status in (200, 404)
    except Exception:
        pass

    results = {
        "cookie_file_active": bool(cf),
        "cookie_path": cf,
        "pot_provider_active": pot_active,
    }
    clients_to_test = [
        [],  # default yt-dlp
        ["web"],
        ["android"],
        ["ios"],
    ]
    for clients in clients_to_test:
        key = ",".join(clients) if clients else "default"
        ext_args = _extractor_args().copy()
        if clients:
            ext_args["youtube"] = {"player_client": clients}
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "cookiefile": cf,
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
            "extractor_args": ext_args if ext_args else None,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=False)
                fmts = info.get("formats") or []
                heights = sorted(set(f["height"] for f in fmts if f.get("height")))
                results[key] = {"ok": True, "title": info.get("title")[:40], "heights": heights, "formats_count": len(fmts)}
        except Exception as e:
            results[key] = {"ok": False, "error": str(e)[:200]}

    return web.json_response(results)


async def handle_download(request: web.Request) -> web.StreamResponse:
    store: Store = request.app["store"]
    token = request.match_info.get("token", "")
    media = store.get_web_media(token)
    if not media:
        return web.Response(
            status=404,
            text="Media not found or download link expired.",
            content_type="text/plain",
        )
    file_path = Path(media.file_path)
    if not file_path.is_file():
        return web.Response(
            status=404,
            text="Media file no longer available on disk.",
            content_type="text/plain",
        )
    return web.FileResponse(
        file_path,
        headers={"Content-Disposition": f'attachment; filename="{media.filename}"'},
    )


async def handle_watch(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    token = request.match_info.get("token", "")
    media = store.get_web_media(token)
    if not media:
        return web.Response(
            status=404,
            text="Media not found or link expired.",
            content_type="text/plain",
        )
    file_path = Path(media.file_path)
    if not file_path.is_file():
        return web.Response(
            status=404,
            text="Media file no longer available on disk.",
            content_type="text/plain",
        )

    download_url = f"/dl/{token}"
    safe_title = html.escape(media.title)
    is_audio = media.content_type.startswith("audio")
    if is_audio:
        player_tag = f'<audio controls autoplay preload="metadata"><source src="{download_url}" type="{media.content_type}">Your browser does not support audio playback.</audio>'
        badge_text = "Audio Stream"
    else:
        player_tag = f'<video controls autoplay playsinline preload="metadata"><source src="{download_url}" type="{media.content_type}">Your browser does not support video playback.</video>'
        badge_text = "Video Stream"

    html_content = HTML_PLAYER_TEMPLATE.format(
        title=safe_title,
        player_tag=player_tag,
        badge_text=badge_text,
        size_text=human_bytes(media.size),
        download_url=download_url,
    )
    return web.Response(text=html_content, content_type="text/html")


async def periodic_cleanup(store: Store, interval_seconds: int = 3600) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            purged = store.purge_expired_web_media(max_age_hours=24)
            if purged:
                logger.info("Purged %d expired web media files from disk", purged)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error during expired media cleanup")


async def start_pot_server() -> asyncio.subprocess.Process | None:
    candidates = [
        Path("/pot_server/build/main.js"),
        Path("/pot_server/dist/main.js"),
        Path("/pot_server/main.js"),
    ]
    server_main = next((p for p in candidates if p.is_file()), None)
    if not server_main:
        logger.info("POT provider server script not found on disk, skipping auto-start")
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(server_main),
            "--host",
            "127.0.0.1",
            "--port",
            "4416",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.info("Started bgutil POT provider on 127.0.0.1:4416 (pid=%d)", proc.pid)
        await asyncio.sleep(1.5)
        return proc
    except Exception as exc:
        logger.warning("Failed to start bgutil POT provider: %s", exc)
        return None


async def run() -> None:
    pot_proc = await start_pot_server()
    config = Config.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    config.web_media_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.database_path)
    timeout = aiohttp.ClientTimeout(
        total=None, connect=30, sock_read=config.poll_timeout + 30
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        telegram = Telegram(config.bot_token, config.bot_api_base, session)
        bot = Bot(config, telegram, store)
        app = web.Application()
        app["store"] = store
        app.router.add_get("/health", health)
        app.router.add_get("/diag", diag)
        app.router.add_get("/dl/{token}", handle_download)
        app.router.add_get("/watch/{token}", handle_watch)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", config.port)
        await site.start()
        cleanup_task = asyncio.create_task(periodic_cleanup(store))
        try:
            await bot.run()
        finally:
            cleanup_task.cancel()
            await runner.cleanup()
            if pot_proc and pot_proc.returncode is None:
                try:
                    pot_proc.terminate()
                except Exception:
                    pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
