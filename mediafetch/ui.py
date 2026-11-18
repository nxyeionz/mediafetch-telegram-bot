from __future__ import annotations

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
