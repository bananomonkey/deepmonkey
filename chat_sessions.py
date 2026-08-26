import asyncio
import logging
import time
import uuid
from typing import Dict, List, Optional

import config
import database

logger = logging.getLogger(__name__)

TABLE = "chats"

DEFAULT_CHAT_NAME = "Чат 1"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class ChatSessionStore:
    """
    Несколько независимых диалогов (чатов) на одного пользователя в личке —
    как отдельные вкладки в ChatGPT. Каждый чат — своя история сообщений
    для DeepSeek, полностью изолированная от остальных чатов пользователя.
    Хранится в SQLite — переживает деплой.
    """

    def __init__(self, max_chats_per_user: int, max_history_turns: int):
        self.max_chats_per_user = max_chats_per_user
        self.max_history_turns = max_history_turns
        self._lock = asyncio.Lock()
        self._data: Dict[int, dict] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            raw = database.load_table(TABLE)
            self._data = {int(uid): rec for uid, rec in raw.items()}
            logger.info("Загружены чаты для %d пользователей из БД", len(self._data))
            self._migrate()
        except Exception as e:
            logger.error("Не удалось прочитать чаты из БД: %s", e)

    def _migrate(self) -> None:
        changed = False
        for uid, rec in self._data.items():
            if len(rec.get("chats", {})) < config.MIN_CHATS_PER_USER:
                while len(rec.get("chats", {})) < config.MIN_CHATS_PER_USER:
                    chat_id = self._new_chat_id()
                    rec.setdefault("chats", {})[chat_id] = {
                        "name": f"Чат {self._next_chat_number(rec)}",
                        "created": _now_iso(),
                        "history": [],
                    }
                if rec.get("active") not in rec["chats"]:
                    rec["active"] = next(iter(rec["chats"].keys()))
                changed = True
                logger.info("Миграция: пользователь %s получил чаты до минимума (%d)", uid, config.MIN_CHATS_PER_USER)
            if self._renumber_chats(rec):
                changed = True
        if changed:
            try:
                database.save_table(TABLE, {str(uid): rec for uid, rec in self._data.items()})
            except Exception as e:
                logger.error("Не удалось сохранить миграцию чатов в БД: %s", e)

    @staticmethod
    def _renumber_chats(rec: dict) -> bool:
        """Перенумеровывает чаты последовательно (Чат 1, Чат 2, ...) по времени создания.
        Чинит дубли вида «Чат 4, Чат 4», оставшиеся от старого кода (len(chats)+1)."""
        chats = rec.get("chats", {})
        if not chats:
            return False
        ordered = sorted(chats.items(), key=lambda kv: kv[1].get("created", ""))
        changed = False
        for i, (cid, c) in enumerate(ordered, start=1):
            expected = f"Чат {i}"
            if c.get("name") != expected:
                c["name"] = expected
                changed = True
        return changed

    @staticmethod
    def _new_chat_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _next_chat_number(rec: dict) -> int:
        used = set()
        for c in rec.get("chats", {}).values():
            name = c.get("name", "")
            if name.startswith("Чат "):
                try:
                    used.add(int(name[4:]))
                except ValueError:
                    pass
        n = 1
        while n in used:
            n += 1
        return n

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

    async def ensure_user(self, user_id: int) -> None:
        async with self._lock:
            self._ensure_user(user_id)
            await self._save_to_db()

    async def append_message(self, user_id: int, role: str, content: str) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            chat = rec["chats"][rec["active"]]
            chat["history"].append({"role": role, "content": content})
            max_messages = self.max_history_turns * 2
            if len(chat["history"]) > max_messages:
                chat["history"] = chat["history"][-max_messages:]
            await self._save_to_db()

    async def create_chat(self, user_id: int, name: Optional[str] = None) -> Optional[str]:
        async with self._lock:
            rec = self._ensure_user(user_id)
            if len(rec["chats"]) >= self.max_chats_per_user:
                return None
            chat_id = self._new_chat_id()
            chat_number = self._next_chat_number(rec)
            rec["chats"][chat_id] = {
                "name": name or f"Чат {chat_number}",
                "created": _now_iso(),
                "history": [],
            }
            rec["active"] = chat_id
            await self._save_to_db()
            return chat_id

    async def switch_chat(self, user_id: int, chat_id: str) -> bool:
        async with self._lock:
            rec = self._ensure_user(user_id)
            if chat_id not in rec["chats"]:
                return False
            rec["active"] = chat_id
            await self._save_to_db()
            return True

    async def delete_chat(self, user_id: int, chat_id: str) -> bool:
        async with self._lock:
            rec = self._ensure_user(user_id)
            if chat_id not in rec["chats"]:
                return False
            if len(rec["chats"]) <= config.MIN_CHATS_PER_USER:
                return False
            del rec["chats"][chat_id]
            if rec["active"] == chat_id:
                rec["active"] = next(iter(rec["chats"].keys()))
            self._renumber_chats(rec)
            await self._save_to_db()
            return True

    async def clear_active_chat(self, user_id: int) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            rec["chats"][rec["active"]]["history"] = []
            await self._save_to_db()

    async def _save_to_db(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_db)
        except Exception as e:
            logger.error("Не удалось сохранить чаты в БД: %s", e)

    def _write_db(self) -> None:
        database.save_table(TABLE, {str(uid): rec for uid, rec in self._data.items()})


chat_sessions = ChatSessionStore(config.MAX_CHATS_PER_USER, config.HISTORY_MAX_TURNS)
