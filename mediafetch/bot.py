from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .downloader import (
    MediaError,
    MediaInfo,
    cleanup,
    download,
    download_audio,
    probe,
    safe,
    supported_url,
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
        self.active_by_user: dict[int, PendingJob] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.download_slots = asyncio.Semaphore(config.max_concurrent_downloads)

    def authorized(self, user_id: int) -> bool:
        return self.config.public_access or user_id in self.config.allowed_user_ids

    async def run(self) -> None:
        try:
            await self.telegram.call("deleteWebhook", {"drop_pending_updates": False})
            await self.telegram.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "start", "description": "Open Media Fetch"},
                        {"command": "help", "description": "Supported links and usage"},
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
        text = str(message.get("text") or "").strip()
        if not self.authorized(user_id):
            await self.telegram.send_message(chat_id, "🔒 Private bot")
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command == "/start":
            self.awaiting_url.discard(user_id)
            await self.show_panel(user_id, chat_id, START_TEXT, start_keyboard())
            return
        if command == "/help":
            await self.show_panel(user_id, chat_id, HELP_TEXT, start_keyboard())
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
            job = self.pending.get(parts[1])
            if not job or job.user_id != user_id:
                await self.show_panel(user_id, chat_id, "<b>⚠ Selection expired</b>", start_keyboard())
                return
            if parts[2] == "audio":
                task = asyncio.create_task(self.process_job(job, None, is_audio=True))
            else:
                task = asyncio.create_task(self.process_job(job, int(parts[2]), is_audio=False))
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
        active = self.active_by_user.get(user_id)
        if active:
            active.cancel.set()
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
        if user_id in self.active_by_user:
            await self.show_panel(
                user_id,
                chat_id,
                "<b>⚠ One download is already running</b>",
                cancel_keyboard(),
                reply_to_message_id=reply_to_message_id,
            )
            return
        panel_id = await self.show_panel(
            user_id,
            chat_id,
            "<b>🔎 Analysing link…</b>\n\n████░░░░░░",
            cancel_keyboard(),
            reply_to_message_id=reply_to_message_id,
        )
        try:
            info = await probe(url)
        except MediaError:
            logger.exception("Metadata extraction failed")
            await self.show_panel(
                user_id,
                chat_id,
                "<b>⚠ Media unavailable</b>\n\nThe link may be private, login-required, deleted, restricted, or temporarily unavailable.",
                start_keyboard(),
            )
            return
        if info.duration and info.duration > self.config.max_duration_seconds:
            await self.show_panel(
                user_id,
                chat_id,
                "<b>⚠ Video is longer than the configured limit</b>",
                start_keyboard(),
            )
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
        details.extend(["", "Choose video quality:"])
        await self.show_panel(
            user_id, chat_id, "\n".join(details), quality_keyboard(job_id, info.qualities)
        )

    async def process_job(
        self, job: PendingJob, height: int | None, is_audio: bool = False
    ) -> None:
        if job.user_id in self.active_by_user:
            return
        self.pending.pop(job.job_id, None)
        self.active_by_user[job.user_id] = job
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
                is_video = path.suffix.lower() == ".mp4"

                if size > self.config.max_file_bytes:
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
                    caption = format_caption(job.info, height, size, is_video=True)
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
            await self.telegram.delete_message(job.chat_id, job.panel_message_id)
            self.store.clear_panel(job.user_id)
        except MediaError as exc:
            if not job.cancel.is_set():
                logger.warning("Download failed: %s", exc)
                detail = (
                    "Instagram did not provide any audio-enabled rendition for this Reel. "
                    "A silent video was blocked instead of being sent."
                    if "audio rendition" in str(exc).lower()
                    else "The media became unavailable, exceeded the size limit, or the platform rejected the request."
                )
                await self.show_panel(
                    job.user_id,
                    job.chat_id,
                    f"<b>⚠ Download failed</b>\n\n{detail}",
                    start_keyboard(),
                )
        except TelegramError:
            logger.exception("Telegram upload failed")
            await self.show_panel(
                job.user_id,
                job.chat_id,
                "<b>⚠ Telegram upload failed</b>\n\nTry a smaller quality.",
                start_keyboard(),
            )
        finally:
            if slot_acquired:
                self.download_slots.release()
            cleanup(job.directory)
            self.pending.pop(job.job_id, None)
            self.active_by_user.pop(job.user_id, None)
