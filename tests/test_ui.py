from types import SimpleNamespace

import pytest

from mediafetch.ui import (
    Quality,
    format_caption,
    forward_keyboard,
    quality_keyboard,
    start_keyboard,
    truncate_caption,
)


def test_start_is_blue() -> None:
    button = start_keyboard()["inline_keyboard"][0][0]
    assert button["style"] == "primary"


def test_quality_colors() -> None:
    markup = quality_keyboard("job123", [Quality(2160), Quality(1080), Quality(720)])
    buttons = [row[0] for row in markup["inline_keyboard"]]
    assert buttons[0]["style"] == "success"
    assert buttons[1]["style"] == "primary"
    assert "style" not in buttons[2]
    assert buttons[-1]["style"] == "danger"


def test_forward_opens_native_chat_picker() -> None:
    button = forward_keyboard("abc123")["inline_keyboard"][0][0]
    assert button["style"] == "success"
    picker = button["switch_inline_query_chosen_chat"]
    assert picker["allow_user_chats"] is True
    assert picker["allow_group_chats"] is True
    assert picker["allow_channel_chats"] is True
    assert picker["allow_bot_chats"] is False


def test_youtube_caption_shows_quality_first_and_full_title() -> None:
    info = SimpleNamespace(
        platform="YouTube",
        title="Python Full Course for Beginners",
        caption="Learn Python programming from scratch with examples.",
        uploader="Programming Hub",
        duration=3600,
        width=1920,
        height=1080,
    )
    caption = format_caption(info, 1080, 500_000_000)
    assert caption.startswith("🎬 <b>Quality: 1080p</b>\n<b>Python Full Course for Beginners</b>")
    assert "Programming Hub" in caption
    assert "01:00:00" in caption
    assert len(caption) <= 1024


def test_instagram_caption_includes_full_post_text() -> None:
    full_text = "Beautiful sunset over the ocean! 🌅🌴 #ocean #sunset"
    info = SimpleNamespace(
        platform="Instagram",
        title=full_text,
        caption=full_text,
        uploader="wanderlust",
        duration=30,
        width=1080,
        height=1920,
    )
    reel_caption = format_caption(info, None, 15_000_000, is_video=True)
    assert full_text in reel_caption
    assert "🎬 Instagram Reel" in reel_caption
    assert "1920p" in reel_caption

    photo_caption = format_caption(info, None, 2_000_000, is_video=False)
    assert full_text in photo_caption
    assert "📸 Instagram Photo" in photo_caption

    carousel_caption = format_caption(info, None, 10_000_000, item_count=5, is_video=False)
    assert full_text in carousel_caption
    assert "📸 Instagram Carousel • 5 items" in carousel_caption


def test_caption_truncates_safely_under_telegram_limit() -> None:
    huge_text = "X" * 2000
    info = SimpleNamespace(
        platform="Instagram",
        title="Huge Post",
        caption=huge_text,
        uploader="spammer",
        duration=0,
        width=1080,
        height=1080,
    )
    caption = format_caption(info, None, 1_000_000, is_video=False)
    assert len(caption) <= 1024
    assert caption.endswith("…\n\n━━━━━━━━━━━━━━━━━━━━\n📸 Instagram Photo • 1080 × 1080 • 💾 976.6 KB")


@pytest.mark.asyncio
async def test_telegram_send_message_with_reply(monkeypatch) -> None:
    from unittest.mock import AsyncMock
    from mediafetch.telegram import Telegram

    tg = Telegram("test-token", "https://api.telegram.org", AsyncMock())
    mock_call = AsyncMock(return_value={"message_id": 999})
    monkeypatch.setattr(tg, "call", mock_call)
    await tg.send_message(123, "hello", reply_to_message_id=456)
    mock_call.assert_awaited_once_with(
        "sendMessage",
        {
            "chat_id": 123,
            "text": "hello",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": None,
            "reply_to_message_id": 456,
            "allow_sending_without_reply": True,
        },
    )



