from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, base_url: str, session: aiohttp.ClientSession) -> None:
        self._root = f"{base_url}/bot{token}"
        self._session = session

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        async with self._session.post(f"{self._root}/{method}", json=payload or {}) as response:
            data = await response.json(content_type=None)
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'unknown Telegram error')}")
        return data.get("result")

    async def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query", "inline_query"],
            },
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        return await self.call("sendMessage", payload)

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any] | bool:
        return await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            pass

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def answer_inline(self, inline_query_id: str, results: list[dict[str, Any]]) -> None:
        await self.call(
            "answerInlineQuery",
            {
                "inline_query_id": inline_query_id,
                "results": results,
                "cache_time": 0,
                "is_personal": True,
            },
        )

    async def send_photo(
        self,
        chat_id: int,
        path: Path,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        if reply_to_message_id:
            form.add_field("reply_to_message_id", str(reply_to_message_id))
            form.add_field("allow_sending_without_reply", "true")
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup, separators=(",", ":")))
        content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        with path.open("rb") as stream:
            form.add_field("photo", stream, filename=path.name, content_type=content_type)
            async with self._session.post(f"{self._root}/sendPhoto", data=form) as response:
                data = await response.json(content_type=None)
        if not data.get("ok"):
            raise TelegramError(f"sendPhoto: {data.get('description', 'unknown Telegram error')}")
        return data["result"]

    async def send_media_group(
        self,
        chat_id: int,
        paths: list[Path],
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        from contextlib import ExitStack

        media_list = []
        with ExitStack() as stack:
            form = aiohttp.FormData(quote_fields=False)
            form.add_field("chat_id", str(chat_id))
            if reply_to_message_id:
                form.add_field("reply_to_message_id", str(reply_to_message_id))
                form.add_field("allow_sending_without_reply", "true")
            for idx, p in enumerate(paths):
                attach_name = f"file{idx}"
                is_video = p.suffix.lower() == ".mp4"
                entry: dict[str, Any] = {
                    "type": "video" if is_video else "photo",
                    "media": f"attach://{attach_name}",
                }
                if idx == 0 and caption:
                    entry["caption"] = caption
                    entry["parse_mode"] = "HTML"
                if is_video:
                    entry["supports_streaming"] = True
                media_list.append(entry)
                fp = stack.enter_context(p.open("rb"))
                content_type = (
                    "video/mp4"
                    if is_video
                    else ("image/png" if p.suffix.lower() == ".png" else "image/jpeg")
                )
                form.add_field(attach_name, fp, filename=p.name, content_type=content_type)
            form.add_field("media", json.dumps(media_list, separators=(",", ":")))
            async with self._session.post(f"{self._root}/sendMediaGroup", data=form) as response:
                data = await response.json(content_type=None)
        if not data.get("ok"):
            raise TelegramError(
                f"sendMediaGroup: {data.get('description', 'unknown Telegram error')}"
            )
        return data["result"]

    async def send_video(
        self,
        chat_id: int,
        path: Path,
        caption: str,
        reply_markup: dict[str, Any],
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        form.add_field("supports_streaming", "true")
        if reply_to_message_id:
            form.add_field("reply_to_message_id", str(reply_to_message_id))
            form.add_field("allow_sending_without_reply", "true")
        form.add_field("reply_markup", json.dumps(reply_markup, separators=(",", ":")))
        with path.open("rb") as stream:
            form.add_field("video", stream, filename=path.name, content_type="video/mp4")
            async with self._session.post(f"{self._root}/sendVideo", data=form) as response:
                data = await response.json(content_type=None)
        if not data.get("ok"):
            raise TelegramError(f"sendVideo: {data.get('description', 'unknown Telegram error')}")
        return data["result"]

    async def send_audio(
        self,
        chat_id: int,
        path: Path,
        caption: str = "",
        title: str = "",
        performer: str = "",
        duration: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
        if title:
            form.add_field("title", title)
        if performer:
            form.add_field("performer", performer)
        if duration:
            form.add_field("duration", str(duration))
        if reply_to_message_id:
            form.add_field("reply_to_message_id", str(reply_to_message_id))
            form.add_field("allow_sending_without_reply", "true")
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup, separators=(",", ":")))
        with path.open("rb") as stream:
            form.add_field("audio", stream, filename=path.name, content_type="audio/mpeg")
            async with self._session.post(f"{self._root}/sendAudio", data=form) as response:
                data = await response.json(content_type=None)
        if not data.get("ok"):
            raise TelegramError(f"sendAudio: {data.get('description', 'unknown Telegram error')}")
        return data["result"]

