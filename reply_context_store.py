import asyncio
import hashlib
import logging
import time
from typing import Dict, List, Optional

import database

logger = logging.getLogger(__name__)

TABLE = "reply_context"


def _hash_text(text: str) -> str:
    normalized = (text or "").strip()[:500]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


class ReplyContextStore:
    """Контекст инлайн/групповых ответов. Хранится в SQLite."""

    def __init__(self, max_entries: int = 5000, ttl_seconds: int = 60 * 60 * 24 * 3):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._store: Dict[str, dict] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            self._store = database.load_table(TABLE)
            logger.info("Загружено %d записей контекста ответов из БД", len(self._store))
        except Exception as e:
            logger.error("Не удалось прочитать контекст ответов из БД: %s", e)

    def get_context(self, assistant_text: str) -> Optional[dict]:
        key = _hash_text(assistant_text)
        entry = self._store.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > self.ttl_seconds:
            return None
        return entry

    async def save_context(self, assistant_text: str, history: List[Dict[str, str]], user_id: Optional[int]) -> None:
        key = _hash_text(assistant_text)
        async with self._lock:
            self._store[key] = {"history": history, "user_id": user_id, "ts": time.time()}
            if len(self._store) > self.max_entries:
                oldest = sorted(self._store.items(), key=lambda kv: kv[1].get("ts", 0))
                for old_key, _ in oldest[: len(self._store) - self.max_entries]:
                    del self._store[old_key]
            await self._save_to_db()

    async def _save_to_db(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_db)
        except Exception as e:
            logger.error("Не удалось сохранить контекст ответов в БД: %s", e)

    def _write_db(self) -> None:
        database.save_table(TABLE, self._store)


reply_context_store = ReplyContextStore()
