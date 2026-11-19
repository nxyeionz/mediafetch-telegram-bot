from pathlib import Path

from mediafetch.config import Config


def test_config_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_ACCESS", "true")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123, 456")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config = Config.from_env()
    assert config.bot_token == "test-token"
    assert config.public_access is True
    assert config.allowed_user_ids == frozenset({123, 456})
    assert config.max_concurrent_downloads == 8
    assert config.database_path == tmp_path / "state.db"


def test_store_shared_media_types(tmp_path: Path) -> None:
    from mediafetch.store import Store, SharedMedia

    db_file = tmp_path / "test_store.db"
    store = Store(db_file)

    # Add video
    store.add_shared_media(
        "tok_vid",
        SharedMedia(
            owner_id=123,
            file_id="vid_123",
            title="Video Title",
            caption="Video Caption",
            media_type="video",
        ),
    )
    # Add photo
    store.add_shared_media(
        "tok_pho",
        SharedMedia(
            owner_id=123,
            file_id="pho_123",
            title="Photo Title",
            caption="Photo Caption",
            media_type="photo",
        ),
    )

    vid = store.get_shared_media("tok_vid", 123)
    assert vid is not None
    assert vid.file_id == "vid_123"
    assert vid.media_type == "video"

    pho = store.get_shared_media("tok_pho", 123)
    assert pho is not None
    assert pho.file_id == "pho_123"
    assert pho.media_type == "photo"


def test_youtube_cookies_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YOUTUBE_COOKIES", "# Netscape HTTP Cookie File\n.youtube.com TRUE / FALSE 1600000000 test value")
    config = Config.from_env()
    cookie_path = tmp_path / "cookies.txt"
    assert cookie_path.exists()
    assert "youtube.com" in cookie_path.read_text(encoding="utf-8")


