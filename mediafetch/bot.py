from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config
from .downloader import (
    MediaError,
    MediaInfo,
    cleanup,
    download,
    download_audio,
    get_cookie_file,
    probe,
    safe,
    supported_url,
    _video_height,
    _video_stream,
)
from .store import SharedMedia, Store, WebMedia
from .telegram import Telegram, TelegramError
from .ui import (
    HELP_TEXT,
    START_TEXT,
    WAITING_TEXT,
    cancel_keyboard,
    format_audio_caption,
    format_caption,
    forward_keyboard,
    human_bytes,
    quality_keyboard,
    start_keyboard,
)


logger = logging.getLogger(__name__)


@dataclass
class PendingJob:
    job_id: str
    user_id: int
    chat_id: int
    panel_message_id: int
    info: MediaInfo
    directory: Path
    cancel: threading.Event
    reply_to_message_id: int | None = None


class Bot:
    def __init__(self, config: Config, telegram: Telegram, store: Store) -> None:
        self.config = config
        self.telegram = telegram
        self.store = store
        self.awaiting_url: set[int] = set()
        self.pending: dict[str, PendingJob] = {}
        self.active_jobs: dict[str, PendingJob] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.download_slots = asyncio.Semaphore(config.max_concurrent_downloads)
        self.telethon: Any = None
        if self.config.telegram_api_id and self.config.telegram_api_hash:
            try:
                from telethon import TelegramClient

                session_path = str(self.config.data_dir / "telethon_bot.session")
                self.telethon = TelegramClient(
                    session_path,
                    self.config.telegram_api_id,
                    self.config.telegram_api_hash,
                )
            except Exception:
                logger.exception("Failed to initialize Telethon client")

    def authorized(self, user_id: int) -> bool:
        return self.config.public_access or user_id in self.config.allowed_user_ids

    async def run(self) -> None:
        if self.telethon:
            try:
                await self.telethon.start(bot_token=self.config.bot_token)
                logger.info("Telethon MTProto 2GB uploader client started successfully")
            except Exception:
                logger.exception("Failed to start Telethon MTProto client")
        try:
            await self.telegram.call("deleteWebhook", {"drop_pending_updates": False})
            await self.telegram.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Open Media Fetch"},
                        {"command": "help", "description": "Supported links and usage"},
                        {"command": "cookies", "description": "YouTube cookies status and guide"},
                        {"command": "cancel", "description": "Cancel active download"},
                    ]
                },
            )
            await self.telegram.call(
                "setMyDescription",
                {
                    "description": "Free media downloader for public YouTube videos and Shorts, Instagram Reels, and X videos. Cookie-free with quality controls and forwarding."
                },
            )
            await self.telegram.call(
                "setMyShortDescription",
                {"short_description": "Free YouTube, Instagram & X video downloader"},
            )
        except TelegramError:
            logger.exception("Telegram initialization failed")
            raise
        offset = 0
        while True:
            try:
                updates = await self.telegram.get_updates(offset, self.config.poll_timeout)
                for update in updates:
                    offset = max(offset, int(update["update_id"]) + 1)
                    task = asyncio.create_task(self.handle_update(update))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Polling failed; retrying")
                await asyncio.sleep(3)

    async def handle_update(self, update: dict[str, Any]) -> None:
        try:
            if "message" in update:
                await self.handle_message(update["message"])
            elif "callback_query" in update:
                await self.handle_callback(update["callback_query"])
            elif "inline_query" in update:
                await self.handle_inline(update["inline_query"])
        except Exception:
            logger.exception("Update handling failed")

    async def show_panel(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        markup: dict[str, Any],
        reply_to_message_id: int | None = None,
    ) -> int:
        existing = self.store.get_panel(user_id)
        if existing:
            old_chat, message_id = existing
            try:
                await self.telegram.edit_message(old_chat, message_id, text, markup)
                return message_id
            except TelegramError as exc:
                if "message is not modified" in str(exc).lower():
                    return message_id
                self.store.clear_panel(user_id)
        message = await self.telegram.send_message(
            chat_id, text, markup, reply_to_message_id=reply_to_message_id
        )
        message_id = int(message["message_id"])
        self.store.set_panel(user_id, chat_id, message_id)
        return message_id

    async def handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        user_id = int(sender.get("id") or 0)
        chat_id = int(message["chat"]["id"])
        msg_id = int(message.get("message_id") or 0)
        text = str(message.get("text") or "").strip()
        if not self.authorized(user_id):
            await self.telegram.send_message(chat_id, "🔒 Private bot")
            return

        document = message.get("document")
        if document:
            file_name = str(document.get("file_name") or "").lower()
            if file_name.endswith(".txt") or "cookie" in file_name:
                try:
                    file_id = document["file_id"]
                    file_info = await self.telegram.call("getFile", {"file_id": file_id})
                    file_path = file_info.get("file_path")
                    if not file_path:
                        await self.telegram.send_message(
                            chat_id, "❌ Could not retrieve file path from Telegram.", reply_to_message_id=msg_id
                        )
                        return
                    download_url = f"{self.config.bot_api_base}/file/bot{self.config.bot_token}/{file_path}"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(download_url) as resp:
                            if resp.status != 200:
                                await self.telegram.send_message(
                                    chat_id, f"❌ Download failed with HTTP {resp.status}.", reply_to_message_id=msg_id
                                )
                                return
                            content = await resp.read()

                    if len(content) == 0:
                        await self.telegram.send_message(
                            chat_id, "❌ Uploaded file is empty.", reply_to_message_id=msg_id
                        )
                        return

                    dest1 = self.config.data_dir / "cookies.txt"
                    dest1.parent.mkdir(parents=True, exist_ok=True)
                    dest1.write_bytes(content)

                    try:
                        Path("cookies.txt").write_bytes(content)
                    except Exception:
                        pass

                    size_kb = len(content) / 1024
                    await self.telegram.send_message(
                        chat_id,
                        (
                            f"✅ <b>YouTube Cookies Saved Successfully!</b>\n\n"
                            f"📁 File size: <code>{size_kb:.1f} KB</code>\n"
                            f"📍 Saved to persistent storage\n\n"
                            f"🚀 <b>YouTube downloads are now unlocked!</b> Send your video link now to download."
                        ),
                        reply_to_message_id=msg_id,
                    )
                    return
                except Exception as exc:
                    logger.exception("Failed to process uploaded cookie file")
                    await self.telegram.send_message(
                        chat_id, f"❌ Failed to process cookie file: {exc}", reply_to_message_id=msg_id
                    )
                    return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command == "/start":
            self.awaiting_url.discard(user_id)
            await self.show_panel(user_id, chat_id, START_TEXT, start_keyboard())
            return
        if command == "/help":
            await self.show_panel(user_id, chat_id, HELP_TEXT, start_keyboard())
            return
        if command == "/cookies":
            cf = get_cookie_file()
            if cf:
                try:
                    sz = Path(cf).stat().st_size / 1024
                except Exception:
                    sz = 0.0
                cookie_text = (
                    f"🍪 <b>YouTube Cookies: Active ✅</b>\n\n"
                    f"📁 Size: <code>{sz:.1f} KB</code>\n"
                    f"📍 Path: <code>{cf}</code>\n\n"
                    f"To update cookies anytime, just drop a new <code>cookies.txt</code> file here."
                )
            else:
                cookie_text = (
                    "🍪 <b>YouTube Cookies: Not Configured ❌</b>\n\n"
                    "Because this bot is hosted on a high-speed cloud datacenter (Singapore), "
                    "YouTube requires cookie authentication to prevent anti-bot blocking.\n\n"
                    "👉 <b>How to set up in 30 seconds:</b>\n"
                    "1. On Chrome/Brave/Edge browser, install the free extension: <b>'Get cookies.txt LOCALLY'</b>.\n"
                    "2. Go to <b>youtube.com</b> (logged into any Google account).\n"
                    "3. Click the extension icon and click <b>Export</b>.\n"
                    "4. Drop or send the exported <code>cookies.txt</code> file here into this chat!\n\n"
                    "The bot will automatically activate it instantly."
                )
            await self.telegram.send_message(
                chat_id, cookie_text, reply_to_message_id=msg_id
            )
            return
        if command == "/cancel":
            await self.cancel(user_id, chat_id)
            return
        if supported_url(text):
            message_id = int(message["message_id"])
            await self.receive_url(user_id, chat_id, text, reply_to_message_id=message_id)
            return
        if user_id in self.awaiting_url:
            await self.show_panel(
                user_id,
                chat_id,
                "<b>⚠ Unsupported link</b>\n\nSend a public YouTube, Instagram, or X media link.",
                cancel_keyboard(),
            )

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        user_id = int(callback["from"]["id"])
        callback_id = str(callback["id"])
        if not self.authorized(user_id):
            await self.telegram.answer_callback(callback_id, "Private bot")
            return
        await self.telegram.answer_callback(callback_id)
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or user_id)
        if data == "download:start":
            self.awaiting_url.add(user_id)
            await self.show_panel(user_id, chat_id, WAITING_TEXT, cancel_keyboard())
        elif data == "download:cancel":
            await self.cancel(user_id, chat_id)
        elif data.startswith("quality:"):
            parts = data.split(":")
            if len(parts) != 3:
                return
            job_id = parts[1]
            action = parts[2]
            job = self.pending.get(job_id)
            if not job or job.user_id != user_id:
                try:
                    await self.telegram.answer_callback(
                        callback_id, "Selection expired or already running"
                    )
                except Exception:
                    pass
                return
            if action == "cancel":
                job.cancel.set()
                cleanup(job.directory)
                self.pending.pop(job_id, None)
                try:
                    await self.telegram.delete_message(chat_id, job.panel_message_id)
                except Exception:
                    pass
                return
            if action == "audio":
                task = asyncio.create_task(self.process_job(job, None, is_audio=True))
            elif action == "best":
                task = asyncio.create_task(self.process_job(job, None, is_audio=False))
            else:
                try:
                    height = int(action)
                except ValueError:
                    height = None
                task = asyncio.create_task(self.process_job(job, height, is_audio=False))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def handle_inline(self, query: dict[str, Any]) -> None:
        user_id = int(query["from"]["id"])
        query_id = str(query["id"])
        text = str(query.get("query") or "")
        if not self.authorized(user_id) or not text.startswith("share_"):
            await self.telegram.answer_inline(query_id, [])
            return
        token = text.removeprefix("share_")
        media = self.store.get_shared_media(token, user_id)
        if not media:
            await self.telegram.answer_inline(query_id, [])
            return
        if media.media_type == "photo":
            result = {
                "type": "photo",
                "id": token,
                "photo_file_id": media.file_id,
                "title": media.title,
                "caption": media.caption,
                "parse_mode": "HTML",
            }
        elif media.media_type == "audio":
            result = {
                "type": "audio",
                "id": token,
                "audio_file_id": media.file_id,
                "title": media.title,
                "caption": media.caption,
                "parse_mode": "HTML",
            }
        else:
            result = {
                "type": "video",
                "id": token,
                "video_file_id": media.file_id,
                "title": media.title,
                "caption": media.caption,
                "parse_mode": "HTML",
            }
        await self.telegram.answer_inline(query_id, [result])

    async def cancel(self, user_id: int, chat_id: int) -> None:
        self.awaiting_url.discard(user_id)
        for job_id, job in list(self.active_jobs.items()):
            if job.user_id == user_id:
                job.cancel.set()
        for job_id, job in list(self.pending.items()):
            if job.user_id == user_id:
                job.cancel.set()
                cleanup(job.directory)
                self.pending.pop(job_id, None)
        await self.show_panel(user_id, chat_id, START_TEXT, start_keyboard())

    async def receive_url(
        self, user_id: int, chat_id: int, url: str, reply_to_message_id: int | None = None
    ) -> None:
        self.awaiting_url.discard(user_id)
        try:
            panel_msg = await self.telegram.send_message(
                chat_id,
                "<b>🔎 Analysing link…</b>\n\n████░░░░░░",
                cancel_keyboard(),
                reply_to_message_id=reply_to_message_id,
            )
            panel_id = int(panel_msg["message_id"])
        except TelegramError:
            logger.exception("Failed to send initial analysis panel")
            return

        try:
            info = await probe(url)
        except MediaError as err:
            logger.exception("Metadata extraction failed: %s", err)
            err_msg = str(err).lower()
            if "bot" in err_msg or "sign in" in err_msg or "cookie" in err_msg:
                panel_text = (
                    "⚠️ <b>YouTube Bot Verification Required</b>\n\n"
                    "YouTube blocked this cloud server IP (<i>Sign in to confirm you’re not a bot</i>).\n\n"
                    "👉 <b>How to unlock in 30 seconds:</b>\n"
                    "1. In your browser (PC/laptop), install free extension: <b>'Get cookies.txt LOCALLY'</b>\n"
                    "2. Open <b>youtube.com</b> (logged into your account) and click <b>Export</b>\n"
                    "3. <b>Send or drag-and-drop the <code>cookies.txt</code> file here into this chat!</b>\n\n"
                    "The bot will automatically save it and unlock YouTube downloads."
                )
            else:
                panel_text = (
                    "<b>⚠ Media unavailable</b>\n\n"
                    "The link may be private, login-required, deleted, restricted, or temporarily unavailable."
                )
            try:
                await self.telegram.edit_message(
                    chat_id,
                    panel_id,
                    panel_text,
                    start_keyboard(),
                )
            except TelegramError:
                pass
            return

        if info.duration and info.duration > self.config.max_duration_seconds:
            try:
                await self.telegram.edit_message(
                    chat_id,
                    panel_id,
                    "<b>⚠ Video is longer than the configured limit</b>",
                    start_keyboard(),
                )
            except TelegramError:
                pass
            return

        job_id = uuid.uuid4().hex[:8]
        job = PendingJob(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            panel_message_id=panel_id,
            info=info,
            directory=self.config.temp_processing_dir / job_id,
            cancel=threading.Event(),
            reply_to_message_id=reply_to_message_id,
        )
        self.pending[job_id] = job

        if info.automatic_best:
            task = asyncio.create_task(self.process_job(job, None))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return

        details = [f"<b>🎬 {safe(info.title)}</b>", "", safe(info.platform)]
        if info.uploader:
            details.append(safe(info.uploader))
        if info.duration:
            details.append(f"Duration: {info.duration // 60:02d}:{info.duration % 60:02d}")
        details.extend(["", "Choose quality:"])
        try:
            await self.telegram.edit_message(
                chat_id,
                panel_id,
                "\n".join(details),
                quality_keyboard(job_id, info.qualities),
            )
        except TelegramError:
            pass

    async def process_job(
        self, job: PendingJob, height: int | None, is_audio: bool = False
    ) -> None:
        if job.job_id in self.active_jobs:
            return
        self.pending.pop(job.job_id, None)
        self.active_jobs[job.job_id] = job
        label = "MP3 (320kbps)" if is_audio else (f"{height}p" if height else "highest available")
        slot_acquired = False

        async def progress(downloaded: int, total: int | None, speed: float | None) -> None:
            if job.cancel.is_set():
                return
            percent = min(99, int(downloaded * 100 / total)) if total else 0
            filled = percent // 10
            bar = "█" * filled + "░" * (10 - filled)
            lines = ["<b>⬇ Downloading</b>", "", f"{bar} {percent}%" if total else bar]
            if total:
                lines.append(f"{human_bytes(downloaded)} / {human_bytes(total)}")
            if speed:
                lines.append(f"Speed: {human_bytes(int(speed))}/s")
            lines.extend(["", f"Quality: {label}"])
            try:
                await self.telegram.edit_message(
                    job.chat_id, job.panel_message_id, "\n".join(lines), cancel_keyboard()
                )
            except TelegramError:
                pass

        try:
            if self.download_slots.locked():
                await self.telegram.edit_message(
                    job.chat_id,
                    job.panel_message_id,
                    "<b>⏳ Added to queue</b>\n\nYour download will start automatically.",
                    cancel_keyboard(),
                )
            await self.download_slots.acquire()
            slot_acquired = True
            if job.cancel.is_set():
                return
            await self.telegram.edit_message(
                job.chat_id,
                job.panel_message_id,
                f"<b>⬇ Downloading</b>\n\n░░░░░░░░░░ 0%\n\nQuality: {label}",
                cancel_keyboard(),
            )
            if is_audio:
                path = await download_audio(
                    job.info,
                    job.directory,
                    job.cancel,
                    progress,
                )
                paths = [path]
            else:
                paths = await download(
                    job.info,
                    height,
                    job.directory,
                    self.config.max_web_file_bytes,
                    job.cancel,
                    progress,
                )
            if job.cancel.is_set():
                return
            if not paths:
                raise MediaError("No media files were downloaded")
            await self.telegram.edit_message(
                job.chat_id,
                job.panel_message_id,
                "<b>📤 Uploading to Telegram</b>\n\n█████████░ 95%",
                cancel_keyboard(),
            )
            token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
            reply_to = job.reply_to_message_id
            if len(paths) == 1:
                path = paths[0]
                size = path.stat().st_size
                is_video = path.suffix.lower() in {".mp4", ".mkv", ".webm"}
                actual_height = (_video_height(path) if is_video else None) or height

                if size > self.config.max_file_bytes:
                    uploaded_via_telethon = False
                    if self.telethon:
                        try:
                            if not self.telethon.is_connected():
                                await self.telethon.connect()
                            if not await self.telethon.is_user_authorized():
                                await self.telethon.start(bot_token=self.config.bot_token)
                            await self.telegram.edit_message(
                                job.chat_id,
                                job.panel_message_id,
                                f"<b>📤 Uploading to Telegram (Direct 2GB MTProto)</b>\n\nSize: {human_bytes(size)}\n░░░░░░░░░░ 0%",
                                cancel_keyboard(),
                            )
                            start_up_time = time.monotonic()
                            last_telethon_up = 0.0

                            async def telethon_progress(current: int, total: int) -> None:
                                try:
                                    nonlocal last_telethon_up
                                    if job.cancel.is_set():
                                        return
                                    now = time.monotonic()
                                    if now - last_telethon_up >= 1.5:
                                        last_telethon_up = now
                                        pct = min(99, int(current * 100 / total)) if total else 0
                                        filled_b = pct // 10
                                        prog = "█" * filled_b + "░" * (10 - filled_b)
                                        elapsed = max(0.1, now - start_up_time)
                                        speed = current / elapsed
                                        await self.telegram.edit_message(
                                            job.chat_id,
                                            job.panel_message_id,
                                            f"<b>⚡ Uploading to Telegram (Turbo {human_bytes(int(speed))}/s)</b>\n\n"
                                            f"{prog} {pct}%\n"
                                            f"{human_bytes(current)} / {human_bytes(total)}",
                                            cancel_keyboard(),
                                        )
                                except Exception:
                                    pass

                            caption_text = (
                                format_audio_caption(job.info, size)
                                if is_audio
                                else format_caption(job.info, actual_height, size, is_video=is_video)
                            )
                            attrs = []
                            if is_video:
                                from telethon.tl.types import DocumentAttributeVideo
                                st = _video_stream(path)
                                w = int(st["width"]) if (st and st.get("width")) else 1920
                                h = int(st["height"]) if (st and st.get("height")) else (actual_height or 1080)
                                dur = float(job.info.duration or 0)
                                attrs.append(DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True))

                            try:
                                peer = await self.telethon.get_input_entity(job.chat_id)
                            except Exception:
                                peer = job.chat_id
                            logger.info("Uploading %s (size=%d bytes, parallel Turbo MTProto) via Telethon to entity %s", path.name, size, peer)
                            try:
                                from .fast_telethon import fast_upload_file
                                file_handle = await fast_upload_file(
                                    self.telethon,
                                    path,
                                    name=path.name,
                                    progress_callback=telethon_progress,
                                    connection_count=12,
                                    part_size_kb=512,
                                )
                            except Exception:
                                logger.exception("Fast parallel upload failed, falling back to standard upload")
                                file_handle = await self.telethon.upload_file(
                                    path,
                                    file_name=path.name,
                                    part_size_kb=512,
                                    progress_callback=telethon_progress,
                                )
                            await self.telethon.send_file(
                                entity=peer,
                                file=file_handle,
                                caption=caption_text,
                                parse_mode="html",
                                attributes=attrs if attrs else None,
                                supports_streaming=is_video,
                                reply_to=reply_to,
                            )
                            uploaded_via_telethon = True
                            logger.info("Telethon direct upload succeeded for %s", path.name)
                        except Exception:
                            logger.exception("Telethon direct upload failed, falling back to web server")

                    if not uploaded_via_telethon:
                        web_token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
                        self.config.web_media_dir.mkdir(parents=True, exist_ok=True)
                        dest_file = self.config.web_media_dir / f"{web_token}_{path.name}"
                        shutil.move(str(path), str(dest_file))

                        content_type = (
                            "audio/mpeg"
                            if is_audio
                            else ("video/mp4" if is_video else "application/octet-stream")
                        )
                        self.store.add_web_media(
                            web_token,
                            WebMedia(
                                token=web_token,
                                file_path=str(dest_file),
                                filename=path.name,
                                title=job.info.title,
                                content_type=content_type,
                                size=size,
                                owner_id=job.user_id,
                            ),
                        )

                        watch_url = f"{self.config.web_base_url}/watch/{web_token}"
                        dl_url = f"{self.config.web_base_url}/dl/{web_token}"
                        media_type_name = "Audio" if is_audio else "Video"

                        text = (
                            f"<b>⚡ {media_type_name} Ready ({human_bytes(size)})</b>\n\n"
                            f"🎬 <b>{safe(job.info.title)}</b>\n"
                            f"Quality: <b>{label}</b>\n\n"
                            f"<i>File exceeds Telegram's 50MB limit, so it is hosted directly on your high-speed Singapore server!</i>\n\n"
                            f"▶️ <a href=\"{watch_url}\"><b>Watch / Stream Online</b></a>\n"
                            f"⬇️ <a href=\"{dl_url}\"><b>Direct High-Speed Download</b></a>\n\n"
                            f"⏰ <i>Link valid for 24 hours</i>"
                        )
                        markup = {
                            "inline_keyboard": [
                                [
                                    {"text": "▶️ Watch Online", "url": watch_url},
                                    {"text": "⬇️ Direct Download", "url": dl_url},
                                ],
                                [
                                    {"text": "➕ Download Another", "callback_data": "download:start"},
                                ],
                            ]
                        }
                        await self.telegram.send_message(
                            job.chat_id, text, markup, reply_to_message_id=reply_to
                        )
                elif is_audio:
                    caption = format_audio_caption(job.info, size)
                    message = await self.telegram.send_audio(
                        job.chat_id,
                        path,
                        caption,
                        title=job.info.title[:100],
                        performer=job.info.uploader[:100] if job.info.uploader else None,
                        duration=job.info.duration if job.info.duration else None,
                        reply_markup=forward_keyboard(token),
                        reply_to_message_id=reply_to,
                    )
                    audio_info = message.get("audio") or {}
                    file_id = str(audio_info.get("file_id") or "")
                    if file_id:
                        self.store.add_shared_media(
                            token,
                            SharedMedia(
                                owner_id=job.user_id,
                                file_id=file_id,
                                title=job.info.title[:200],
                                caption=caption,
                                media_type="audio",
                            ),
                        )
                elif is_video:
                    caption = format_caption(job.info, actual_height, size, is_video=True)
                    message = await self.telegram.send_video(
                        job.chat_id,
                        path,
                        caption,
                        forward_keyboard(token),
                        reply_to_message_id=reply_to,
                    )
                    file_id = str(message["video"]["file_id"])
                    self.store.add_shared_media(
                        token,
                        SharedMedia(
                            owner_id=job.user_id,
                            file_id=file_id,
                            title=job.info.title[:200],
                            caption=caption,
                            media_type="video",
                        ),
                    )
                else:
                    caption = format_caption(job.info, height, size, is_video=False)
                    message = await self.telegram.send_photo(
                        job.chat_id,
                        path,
                        caption,
                        forward_keyboard(token),
                        reply_to_message_id=reply_to,
                    )
                    photos = message.get("photo") or []
                    file_id = str(photos[-1]["file_id"]) if photos else ""
                    if file_id:
                        self.store.add_shared_media(
                            token,
                            SharedMedia(
                                owner_id=job.user_id,
                                file_id=file_id,
                                title=job.info.title[:200],
                                caption=caption,
                                media_type="photo",
                            ),
                        )
            else:
                total_size = sum(p.stat().st_size for p in paths)
                caption = format_caption(
                    job.info, height, total_size, item_count=len(paths), is_video=False
                )
                chunk_size = 10
                for i in range(0, len(paths), chunk_size):
                    chunk = paths[i : i + chunk_size]
                    batch_caption = caption if i == 0 else ""
                    chunk_reply_to = reply_to if i == 0 else None
                    await self.telegram.send_media_group(
                        job.chat_id,
                        chunk,
                        caption=batch_caption,
                        reply_to_message_id=chunk_reply_to,
                    )
            try:
                await self.telegram.delete_message(job.chat_id, job.panel_message_id)
            except TelegramError:
                pass
        except MediaError as exc:
            if not job.cancel.is_set():
                logger.warning("Download failed: %s", exc)
                err_str = str(exc).lower()
                if "bot" in err_str or "sign in" in err_str:
                    detail = (
                        "YouTube blocked this download with anti-bot verification.\n\n"
                        "Send a <code>cookies.txt</code> file here to unlock."
                    )
                elif "audio rendition" in err_str and getattr(job.info, "platform", "") == "Instagram":
                    detail = (
                        "Instagram did not provide any audio-enabled rendition for this Reel. "
                        "A silent video was blocked instead of being sent."
                    )
                else:
                    detail = f"The download could not be completed: {safe(str(exc))[:200]}"
                try:
                    await self.telegram.edit_message(
                        job.chat_id,
                        job.panel_message_id,
                        f"<b>⚠ Download failed</b>\n\n{detail}",
                        start_keyboard(),
                    )
                except TelegramError:
                    pass
        except TelegramError:
            logger.exception("Telegram upload failed")
            try:
                await self.telegram.edit_message(
                    job.chat_id,
                    job.panel_message_id,
                    "<b>⚠ Telegram upload failed</b>\n\nTry a smaller quality.",
                    start_keyboard(),
                )
            except TelegramError:
                pass
        finally:
            if slot_acquired:
                self.download_slots.release()
            cleanup(job.directory)
            self.pending.pop(job.job_id, None)
            self.active_jobs.pop(job.job_id, None)
