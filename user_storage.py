import asyncio
import logging
import time
from typing import Dict, Optional, Set

import database

logger = logging.getLogger(__name__)

TABLE = "users"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class UserStorage:
    """
    База пользователей: кто писал боту, короткая заметка ИИ о его интересах
    (профиль для персонализации), бан-статус, счётчик сообщений.
    Хранится в SQLite (database.py) — переживает рестарт и деплой.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._users: Dict[int, dict] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            raw = database.load_table(TABLE)
            self._users = {int(uid): rec for uid, rec in raw.items()}
            logger.info("Загружено %d пользователей из БД", len(self._users))
        except Exception as e:
            logger.error("Не удалось прочитать пользователей из БД: %s", e)

    @staticmethod
    def _blank_record(username: Optional[str] = None, full_name: Optional[str] = None) -> dict:
        return {
            "username": username,
            "full_name": full_name,
            "profile": "",
            "banned": False,
            "message_count": 0,
            "first_seen": _now_iso(),
            "last_seen": _now_iso(),
        }

    def get_all(self) -> Set[int]:
        return set(self._users.keys())

    def count(self) -> int:
        return len(self._users)

    def contains(self, user_id: int) -> bool:
        return user_id in self._users

    def is_banned(self, user_id: int) -> bool:
        rec = self._users.get(user_id)
        return bool(rec and rec.get("banned"))

    def get_record(self, user_id: int) -> Optional[dict]:
        rec = self._users.get(user_id)
        return dict(rec) if rec else None

    def get_profile(self, user_id: int) -> str:
        rec = self._users.get(user_id)
        return rec.get("profile", "") if rec else ""

    async def touch(self, user_id: int, username: Optional[str] = None, full_name: Optional[str] = None) -> None:
        """Регистрирует пользователя (если новый), обновляет счётчик сообщений и last_seen."""
        async with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                rec = self._blank_record(username, full_name)
                self._users[user_id] = rec
            if username:
                rec["username"] = username
            if full_name:
                rec["full_name"] = full_name
            rec["message_count"] = rec.get("message_count", 0) + 1
            rec["last_seen"] = _now_iso()
            await self._save_to_db()

    async def set_profile(self, user_id: int, profile_text: str) -> None:
        async with self._lock:
            rec = self._users.setdefault(user_id, self._blank_record())
            rec["profile"] = profile_text
            await self._save_to_db()

    async def ban(self, user_id: int) -> bool:
        """True, если пользователь найден в базе и забанен."""
        async with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return False
            rec["banned"] = True
            await self._save_to_db()
            return True

    async def unban(self, user_id: int) -> bool:
        async with self._lock:
            rec = self._users.get(user_id)
            if rec is None:
                return False
            rec["banned"] = False
            await self._save_to_db()
            return True

    async def remove(self, user_id: int) -> None:
        async with self._lock:
            if user_id in self._users:
                del self._users[user_id]
                await self._save_to_db()

    async def _save_to_db(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_db)
        except Exception as e:
            logger.error("Не удалось сохранить пользователей в БД: %s", e)

    def _write_db(self) -> None:
        database.save_table(TABLE, {str(uid): rec for uid, rec in self._users.items()})


user_storage = UserStorage()
