import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)


def _hash_text(text: str) -> str:
    normalized = (text or "").strip()[:500]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


class ReplyContextStore:
    """
    Позволяет ИИ "помнить" ветку переписки там, где нет единого постоянного
    чата с пользователем (инлайн-режим, группы): когда кто-то отвечает
    (reply) на предыдущее сообщение бота, мы находим контекст этого
    сообщения и продолжаем диалог с учётом истории.

    Технически Telegram не даёт нам заранее знать chat_id/message_id
    инлайн-сообщения (в chosen_inline_result есть только inline_message_id,
    непригодный для сопоставления с последующим reply_to_message) — поэтому
    контекст ищется по хэшу ТЕКСТА последнего ответа ИИ. Это простое и
    рабочее решение, но не абсолютно надёжное: если два разных ответа ИИ
    окажутся текстуально идентичны, их контексты могут перепутаться —
    на практике вероятность этого крайне мала (ответы ИИ почти всегда
    уникальны по формулировке).
    """

    def __init__(self, file_path: str, max_entries: int = 5000, ttl_seconds: int = 60 * 60 * 24 * 3):
        self.file_path = file_path
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._store: Dict[str, dict] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                self._store = json.load(f)
            logger.info("Загружено %d записей контекста ответов из %s", len(self._store), self.file_path)
        except Exception as e:
            logger.error("Не удалось прочитать файл контекста ответов (%s): %s", self.file_path, e)

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
            await self._save_to_disk()

    async def _save_to_disk(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_file)
        except Exception as e:
            logger.error("Не удалось сохранить файл контекста ответов (%s): %s", self.file_path, e)

    def _write_file(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self._store, f, ensure_ascii=False)


reply_context_store = ReplyContextStore(config.REPLY_CONTEXT_FILE_PATH)
