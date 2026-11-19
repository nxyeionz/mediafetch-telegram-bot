from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any


START_TEXT = """<b>MEDIA FETCH</b>
YouTube • Instagram • X

Download public videos, reels, and posts directly inside Telegram.

<b>YouTube Videos</b>
Choose the required quality.

<b>YouTube Shorts</b>
Highest quality with source audio automatically.

<b>Instagram Reels & Posts</b>
Highest quality videos, photos, and carousels automatically.

<b>X Videos</b>
Highest quality with source audio automatically.

🆓 Free for everyone
🍪 Cookie-free  •  🧹 Automatic cleanup"""

WAITING_TEXT = """<b>🔗 Send your media link</b>

Supported:
• YouTube videos and Shorts
• Instagram Reels, photos, and carousel posts
• X videos

Send one public link below."""

HELP_TEXT = """<b>How Media Fetch works</b>

1. Press Download Video
2. Send a public link
3. Choose quality for regular YouTube videos
4. Shorts, Instagram (Reels/posts) and X use highest quality automatically
5. Receive your media (video, photo, or carousel)
6. Press Forward to share it

Private, login-required, deleted and region-restricted media are unavailable."""


def button(text: str, *, style: str | None = None, **action: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"text": text, **action}
    if style:
        result["style"] = style
    return result


def keyboard(*rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"inline_keyboard": list(rows)}


def start_keyboard() -> dict[str, Any]:
    return keyboard([button("⬇ Download Video", callback_data="download:start", style="primary")])


def cancel_keyboard() -> dict[str, Any]:
    return keyboard([button("✖ Cancel", callback_data="download:cancel", style="danger")])


def forward_keyboard(token: str) -> dict[str, Any]:
    return keyboard(
        [
            button(
                "↗ Forward",
                style="success",
                switch_inline_query_chosen_chat={
                    "query": f"share_{token}",
                    "allow_user_chats": True,
                    "allow_bot_chats": False,
                    "allow_group_chats": True,
                    "allow_channel_chats": True,
                },
            )
        ]
    )


@dataclass(frozen=True)
class Quality:
    height: int
    estimated_bytes: int | None = None


def human_bytes(value: int | None) -> str:
    if value is None:
        return ""
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


def quality_keyboard(job_id: str, qualities: list[Quality]) -> dict[str, Any]:
    if not qualities:
        return cancel_keyboard()
    ordered = sorted(qualities, key=lambda item: item.height, reverse=True)
    highest = ordered[0].height
    rows: list[list[dict[str, Any]]] = []
    for item in ordered:
        label = f"{item.height}p"
        style = None
        if item.height == highest:
            label += " • Best"
            style = "success"
        if item.height == 1080:
            label += " + Recommended" if item.height == highest else " • Recommended"
            if style is None:
                style = "primary"
        size = human_bytes(item.estimated_bytes)
        if size:
            label += f" • ~{size}"
        rows.append([button(label, callback_data=f"quality:{job_id}:{item.height}", style=style)])
    rows.append([button("✖ Cancel", callback_data="download:cancel", style="danger")])
    return keyboard(*rows)


def truncate_caption(body: str, max_chars: int) -> str:
    escaped = html.escape(body, quote=False)
    if len(escaped) <= max_chars:
        return escaped
    cur = body[:max_chars]
    while cur and len(html.escape(cur + "…", quote=False)) > max_chars:
        cur = cur[:-1]
    return html.escape(cur, quote=False) + "…"


def format_caption(
    info: Any,
    height: int | None,
    size: int,
    *,
    item_count: int = 1,
    is_video: bool = True,
) -> str:
    if height:
        quality_str = f"{height}p"
    elif getattr(info, "height", None):
        quality_str = f"{info.height}p"
    else:
        quality_str = "Best"

    size_str = human_bytes(size)
    duration = getattr(info, "duration", 0) or 0
    if duration:
        if duration >= 3600:
            duration_str = f"{duration // 3600:02d}:{(duration % 3600) // 60:02d}:{duration % 60:02d}"
        else:
            duration_str = f"{duration // 60:02d}:{duration % 60:02d}"
    else:
        duration_str = ""
    width = getattr(info, "width", None)
    h = getattr(info, "height", None)
    dim_str = f"{width} × {h}" if width and h else ""
    platform = getattr(info, "platform", "Media")
    title = (getattr(info, "title", "") or "").strip()
    full_caption = (getattr(info, "caption", "") or "").strip()
    content_text = full_caption or title

    sep = " • "

    if platform == "YouTube":
        header = f"🎬 <b>Quality: {quality_str}</b>\n"
        meta_parts = []
        if duration_str:
            meta_parts.append(f"⏱ {duration_str}")
        if size_str:
            meta_parts.append(f"💾 {size_str}")
        uploader = (getattr(info, "uploader", "") or "").strip()
        if uploader:
            meta_parts.append(f"👤 {html.escape(uploader, quote=False)}")
        footer_line = sep.join(meta_parts)
        footer = f"\n\n{footer_line}" if footer_line else ""

        body_text = f"<b>{html.escape(title, quote=False)}</b>"
        if full_caption and full_caption != title:
            clean_desc = "\n".join(
                line for line in full_caption.splitlines() if not line.strip().startswith("http")
            ).strip()
            if clean_desc and clean_desc != title:
                desc_snippet = clean_desc[:300]
                body_text += f"\n\n{html.escape(desc_snippet, quote=False)}"

        max_len = 1024 - len(header) - len(footer)
        if len(body_text) > max_len:
            body_text = f"<b>{truncate_caption(title, max_len - 10)}</b>"
        return f"{header}{body_text}{footer}".strip()

    elif platform == "Instagram":
        if item_count > 1:
            meta_str = f"📸 Instagram Carousel • {item_count} items • 💾 {size_str}"
        elif not is_video:
            parts = ["📸 Instagram Photo"]
            if dim_str:
                parts.append(dim_str)
            if size_str:
                parts.append(f"💾 {size_str}")
            meta_str = sep.join(parts)
        else:
            parts = ["🎬 Instagram Reel", quality_str]
            if duration_str:
                parts.append(f"⏱ {duration_str}")
            if size_str:
                parts.append(f"💾 {size_str}")
            meta_str = sep.join(parts)

        footer = f"\n\n━━━━━━━━━━━━━━━━━━━━\n{meta_str}"
        max_body_len = 1024 - len(footer)
        if content_text:
            body = truncate_caption(content_text, max_body_len)
            return f"{body}{footer}".strip()
        else:
            return meta_str

    elif platform == "X":
        parts = ["🐦 X Video", quality_str]
        if duration_str:
            parts.append(f"⏱ {duration_str}")
        if size_str:
            parts.append(f"💾 {size_str}")
        meta_str = sep.join(parts)
        footer = f"\n\n━━━━━━━━━━━━━━━━━━━━\n{meta_str}"
        max_body_len = 1024 - len(footer)
        if content_text:
            body = truncate_caption(content_text, max_body_len)
            return f"{body}{footer}".strip()
        else:
            return meta_str

    else:
        parts = [platform, quality_str]
        if duration_str:
            parts.append(duration_str)
        if size_str:
            parts.append(size_str)
        meta_str = sep.join(parts)
        footer = f"\n\n━━━━━━━━━━━━━━━━━━━━\n{meta_str}"
        max_body_len = 1024 - len(footer)
        body = truncate_caption(content_text or title, max_body_len)
        return f"{body}{footer}".strip()
