import asyncio
import logging
from typing import Dict, List

import config
import database

logger = logging.getLogger(__name__)

TABLE = "group_chats"


class GroupChatSessions:
    """История сообщений ИИ отдельно для каждой группы.

    Контекст в общих чатах полностью изолирован от ЛС бота: ключ — chat_id
    группы, а не user_id. Каждая группа ведёт свой независимый диалог.
    """

    def __init__(self, max_history_turns: int):
        self.max_history_turns = max_history_turns
        self._lock = asyncio.Lock()
        self._data: Dict[str, dict] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            self._data = database.load_table(TABLE)
            logger.info("Загружена история для %d групп из БД", len(self._data))
        except Exception as e:
            logger.error("Не удалось прочитать историю групп из БД: %s", e)

    def get_history(self, chat_id: int) -> List[dict]:
        rec = self._data.get(str(chat_id))
        return list(rec["history"]) if rec else []

    async def append_message(self, chat_id: int, role: str, content: str) -> None:
        async with self._lock:
            key = str(chat_id)
            rec = self._data.get(key) or {"history": []}
            rec["history"].append({"role": role, "content": content})
            max_messages = self.max_history_turns * 2
            if len(rec["history"]) > max_messages:
                rec["history"] = rec["history"][-max_messages:]
            self._data[key] = rec
            await self._save_to_db()

    async def clear_chat(self, chat_id: int) -> None:
        async with self._lock:
            self._data[str(chat_id)] = {"history": []}
            await self._save_to_db()

    async def _save_to_db(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_db)
        except Exception as e:
            logger.error("Не удалось сохранить историю групп в БД: %s", e)

    def _write_db(self) -> None:
        database.save_table(TABLE, self._data)


group_sessions = GroupChatSessions(config.HISTORY_MAX_TURNS)
