from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


class CaptionCache:
    """Store description text and metadata only; never image files or bytes."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "captions.sqlite3")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS captions (
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                provider_id TEXT NOT NULL, created_at REAL NOT NULL
            )
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def caption(self, key: str, ttl_hours: int = 0) -> dict | None:
        row = self.db.execute(
            "SELECT value, provider_id, created_at FROM captions WHERE key=?", (key,)
        ).fetchone()
        if not row or (ttl_hours > 0 and row[2] + ttl_hours * 3600 <= time.time()):
            return None
        return {"description": row[0], "provider_id": row[1], "cached": True}

    def save_caption(self, key: str, text: str, provider_id: str):
        self.db.execute(
            "INSERT OR REPLACE INTO captions VALUES(?,?,?,?)",
            (key, text, provider_id, time.time()),
        )
        self.db.commit()

    @staticmethod
    def caption_key(
        channel: str,
        data: bytes,
        providers: tuple[str, ...],
        question: str,
        version: str,
    ) -> str:
        identity = [
            channel,
            hashlib.sha256(data).hexdigest(),
            providers,
            question,
            version,
        ]
        return hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode()
        ).hexdigest()
