from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SharedMedia:
    owner_id: int
    file_id: str
    title: str
    caption: str
    media_type: str = "video"


@dataclass(frozen=True)
class WebMedia:
    token: str
    file_path: str
    filename: str
    title: str
    content_type: str
    size: int
    caption: str = ""
    owner_id: int = 0


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS panels (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_media (
                    token TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'video',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS web_media (
                    token TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    title TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            try:
                self._connection.execute(
                    "ALTER TABLE shared_media ADD COLUMN media_type TEXT NOT NULL DEFAULT 'video'"
                )
            except sqlite3.OperationalError:
                pass

    def set_panel(self, user_id: int, chat_id: int, message_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO panels(user_id, chat_id, message_id) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, message_id=excluded.message_id",
                (user_id, chat_id, message_id),
            )

    def get_panel(self, user_id: int) -> tuple[int, int] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT chat_id, message_id FROM panels WHERE user_id = ?", (user_id,)
            ).fetchone()
        return (int(row["chat_id"]), int(row["message_id"])) if row else None

    def clear_panel(self, user_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM panels WHERE user_id = ?", (user_id,))

    def add_shared_media(self, token: str, media: SharedMedia) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO shared_media(token, owner_id, file_id, title, caption, media_type) VALUES(?, ?, ?, ?, ?, ?)",
                (token, media.owner_id, media.file_id, media.title, media.caption, media.media_type),
            )

    def get_shared_media(self, token: str, owner_id: int) -> SharedMedia | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT owner_id, file_id, title, caption, media_type FROM shared_media WHERE token = ? AND owner_id = ?",
                (token, owner_id),
            ).fetchone()
        if not row:
            return None
        return SharedMedia(
            owner_id=int(row["owner_id"]),
            file_id=str(row["file_id"]),
            title=str(row["title"]),
            caption=str(row["caption"]),
            media_type=str(row["media_type"]) if "media_type" in row.keys() else "video",
        )

    def add_web_media(
        self, token_or_media: str | WebMedia, media: WebMedia | None = None
    ) -> None:
        if isinstance(token_or_media, WebMedia):
            m = token_or_media
            tok = m.token
        else:
            assert media is not None
            m = media
            tok = token_or_media
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO web_media(token, file_path, filename, title, caption, content_type, size) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    tok,
                    m.file_path,
                    m.filename,
                    m.title,
                    m.caption,
                    m.content_type,
                    m.size,
                ),
            )

    def get_web_media(self, token: str) -> WebMedia | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT token, file_path, filename, title, caption, content_type, size "
                "FROM web_media WHERE token = ?",
                (token,),
            ).fetchone()
        if not row:
            return None
        return WebMedia(
            token=str(row["token"]),
            file_path=str(row["file_path"]),
            filename=str(row["filename"]),
            title=str(row["title"]),
            content_type=str(row["content_type"]),
            size=int(row["size"]),
            caption=str(row["caption"] or ""),
        )

    def purge_expired_web_media(self, max_age_hours: int = 24) -> list[str]:
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT token, file_path FROM web_media "
                "WHERE created_at < datetime('now', ?)",
                (f"-{max_age_hours} hours",),
            ).fetchall()
            expired_paths: list[str] = []
            for row in rows:
                p = Path(row["file_path"])
                p.unlink(missing_ok=True)
                expired_paths.append(str(p))
            self._connection.execute(
                "DELETE FROM web_media WHERE created_at < datetime('now', ?)",
                (f"-{max_age_hours} hours",),
            )
            return expired_paths

