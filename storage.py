from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


class Cache:
    """Lazy file and description caches; never an incoming-message image index."""

    def __init__(self, directory: Path, files: Path, ttl_seconds: int, max_bytes: int):
        directory.mkdir(parents=True, exist_ok=True)
        files.mkdir(parents=True, exist_ok=True)
        self.files = files.resolve()
        self.ttl = ttl_seconds
        self.max_bytes = max_bytes
        self.db = sqlite3.connect(directory / "cache.sqlite3")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files
                (key TEXT PRIMARY KEY, path TEXT, expires REAL, accessed REAL);
            CREATE TABLE IF NOT EXISTS captions
                (key TEXT PRIMARY KEY, value TEXT, expires REAL);
        """)

    def close(self):
        self.db.close()

    def file(self, key: str) -> Path | None:
        row = self.db.execute(
            "SELECT path, expires FROM files WHERE key=?", (key,)
        ).fetchone()
        if not row or row[1] < time.time():
            return None
        path = Path(row[0]).resolve()
        if not path.is_relative_to(self.files) or not path.is_file():
            return None
        self.db.execute("UPDATE files SET accessed=? WHERE key=?", (time.time(), key))
        self.db.commit()
        return path

    def save_file(
        self, key: str, data: bytes, extension: str, ttl_seconds: int | None = None
    ) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        path = self.files / (digest + extension)
        if not path.exists():
            temporary = self.files / (digest + ".partial")
            temporary.write_bytes(data)
            temporary.replace(path)
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO files VALUES(?,?,?,?)",
            (key, str(path), now + min(ttl_seconds or self.ttl, self.ttl), now),
        )
        self.db.commit()
        self.cleanup(exclude=path)
        return path

    def caption(self, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value, expires FROM captions WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row and row[1] >= time.time() else None

    def save_caption(self, key: str, text: str):
        self.db.execute("DELETE FROM captions WHERE expires<?", (time.time(),))
        self.db.execute(
            "INSERT OR REPLACE INTO captions VALUES(?,?,?)",
            (key, text, time.time() + self.ttl),
        )
        self.db.commit()

    @staticmethod
    def caption_key(
        channel: str, data: bytes, provider: str, question: str, version: str
    ) -> str:
        identity = [
            channel,
            hashlib.sha256(data).hexdigest(),
            provider,
            question,
            version,
        ]
        return hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode()
        ).hexdigest()

    def cleanup(self, exclude: Path | None = None):
        now = time.time()
        self.db.execute("DELETE FROM captions WHERE expires<?", (now,))
        self.db.execute("DELETE FROM files WHERE expires<?", (now,))
        tracked = {
            Path(r[0]) for r in self.db.execute("SELECT DISTINCT path FROM files")
        }
        for path in self.files.iterdir():
            if path != exclude and path.is_file() and path not in tracked:
                path.unlink()
        rows = self.db.execute(
            "SELECT path, MAX(accessed) FROM files GROUP BY path ORDER BY MAX(accessed)"
        ).fetchall()
        total = sum(
            Path(path).stat().st_size for path, _ in rows if Path(path).is_file()
        )
        for raw_path, _ in rows:
            path = Path(raw_path)
            if total <= self.max_bytes:
                break
            if (
                path == exclude
                or not path.is_file()
                or not path.resolve().is_relative_to(self.files)
            ):
                continue
            total -= path.stat().st_size
            path.unlink()
            self.db.execute("DELETE FROM files WHERE path=?", (raw_path,))
        self.db.commit()
