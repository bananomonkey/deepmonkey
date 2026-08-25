import asyncio
import json
import logging
import os
import time
import uuid
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)

DEFAULT_CHAT_NAME = "Чат 1"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class ChatSessionStore:
    """
    Несколько независимых диалогов (чатов) на одного пользователя в личке —
    как отдельные вкладки в ChatGPT. Каждый чат — своя история сообщений
    для DeepSeek, полностью изолированная от остальных чатов пользователя.
    """

    def __init__(self, file_path: str, max_chats_per_user: int, max_history_turns: int):
        self.file_path = file_path
        self.max_chats_per_user = max_chats_per_user
        self.max_history_turns = max_history_turns
        self._lock = asyncio.Lock()
        self._data: Dict[int, dict] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not os.path.exists(self.file_path):
            logger.info("Файл сессий чатов не найден, начинаем с пустого хранилища.")
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._data = {int(uid): rec for uid, rec in raw.items()}
            logger.info("Загружены чаты для %d пользователей из %s", len(self._data), self.file_path)
        except Exception as e:
            logger.error("Не удалось прочитать файл сессий чатов (%s): %s", self.file_path, e)

    @staticmethod
    def _new_chat_id() -> str:
        return uuid.uuid4().hex[:12]

    def _ensure_user(self, user_id: int) -> dict:
        """Вызывать ТОЛЬКО внутри self._lock — мутирует состояние."""
        rec = self._data.get(user_id)
        if rec is None:
            chat_id = self._new_chat_id()
            rec = {
                "active": chat_id,
                "chats": {chat_id: {"name": DEFAULT_CHAT_NAME, "created": _now_iso(), "history": []}},
            }
            self._data[user_id] = rec
        return rec

    # --- Чтение (без мутации, безопасно без лока) ---

    def list_chats(self, user_id: int) -> List[dict]:
        rec = self._data.get(user_id)
        if not rec:
            return []
        active = rec["active"]
        return [
            {"id": cid, "name": c["name"], "active": cid == active}
            for cid, c in rec["chats"].items()
        ]

    def get_history(self, user_id: int) -> List[dict]:
        rec = self._data.get(user_id)
        if not rec:
            return []
        chat = rec["chats"].get(rec["active"])
        return list(chat["history"]) if chat else []

    def get_active_chat_name(self, user_id: int) -> str:
        rec = self._data.get(user_id)
        if not rec:
            return DEFAULT_CHAT_NAME
        chat = rec["chats"].get(rec["active"])
        return chat["name"] if chat else DEFAULT_CHAT_NAME

    def chats_count(self, user_id: int) -> int:
        rec = self._data.get(user_id)
        return len(rec["chats"]) if rec else 0

    # --- Изменение (под локом + persist) ---

    async def ensure_user(self, user_id: int) -> None:
        async with self._lock:
            self._ensure_user(user_id)
            await self._save_to_disk()

    async def append_message(self, user_id: int, role: str, content: str) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            chat = rec["chats"][rec["active"]]
            chat["history"].append({"role": role, "content": content})
            max_messages = self.max_history_turns * 2
            if len(chat["history"]) > max_messages:
                chat["history"] = chat["history"][-max_messages:]
            await self._save_to_disk()

    async def create_chat(self, user_id: int, name: Optional[str] = None) -> Optional[str]:
        async with self._lock:
            rec = self._ensure_user(user_id)
            if len(rec["chats"]) >= self.max_chats_per_user:
                return None
            chat_id = self._new_chat_id()
            chat_number = len(rec["chats"]) + 1
            rec["chats"][chat_id] = {
                "name": name or f"Чат {chat_number}",
                "created": _now_iso(),
                "history": [],
            }
            rec["active"] = chat_id
            await self._save_to_disk()
            return chat_id

    async def switch_chat(self, user_id: int, chat_id: str) -> bool:
        async with self._lock:
            rec = self._ensure_user(user_id)
            if chat_id not in rec["chats"]:
                return False
            rec["active"] = chat_id
            await self._save_to_disk()
            return True

    async def delete_chat(self, user_id: int, chat_id: str) -> bool:
        async with self._lock:
            rec = self._ensure_user(user_id)
            if chat_id not in rec["chats"]:
                return False
            if len(rec["chats"]) == 1:
                # нельзя удалить последний чат — просто очищаем его историю
                rec["chats"][chat_id]["history"] = []
                await self._save_to_disk()
                return True
            del rec["chats"][chat_id]
            if rec["active"] == chat_id:
                rec["active"] = next(iter(rec["chats"].keys()))
            await self._save_to_disk()
            return True

    async def clear_active_chat(self, user_id: int) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            rec["chats"][rec["active"]]["history"] = []
            await self._save_to_disk()

    async def _save_to_disk(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_file)
        except Exception as e:
            logger.error("Не удалось сохранить файл сессий чатов (%s): %s", self.file_path, e)

    def _write_file(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump({str(uid): rec for uid, rec in self._data.items()}, f, ensure_ascii=False)


chat_sessions = ChatSessionStore(config.CHATS_FILE_PATH, config.MAX_CHATS_PER_USER, config.HISTORY_MAX_TURNS)
