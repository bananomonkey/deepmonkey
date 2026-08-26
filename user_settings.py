import asyncio
import logging
from typing import Dict

import database

logger = logging.getLogger(__name__)

TABLE = "user_settings"

SETTINGS_TABLE = "settings"
THINKING_KEY = "thinking_enabled"

DEFAULT_MODEL = "fast"

MODEL_MAP = {
    "fast": "deepseek-v4-flash",
    "thinking": "deepseek-v4-flash",
}

THINKING_PARAM_MAP = {
    "fast": False,
    "thinking": True,
}


class UserSettings:
    """Per-user настройки (модель, системный промпт). Хранятся в SQLite."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._data: Dict[int, dict] = {}
        self._thinking_enabled = True
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            raw = database.load_table(TABLE)
            self._data = {int(uid): rec for uid, rec in raw.items()}
            logger.info("Загружены настройки для %d пользователей из БД", len(self._data))
        except Exception as e:
            logger.error("Не удалось прочитать настройки из БД: %s", e)
        try:
            value = database.get_value(SETTINGS_TABLE, THINKING_KEY)
            if value is not None:
                self._thinking_enabled = bool(value)
                logger.info("Думающая модель глобально: %s", "ВКЛ" if self._thinking_enabled else "ВЫКЛ")
        except Exception as e:
            logger.error("Не удалось прочитать флаг thinking_enabled: %s", e)

    def _ensure_user(self, user_id: int) -> dict:
        if user_id not in self._data:
            self._data[user_id] = {"model": DEFAULT_MODEL, "system_prompt": ""}
        return self._data[user_id]

    def get_model(self, user_id: int) -> str:
        rec = self._data.get(user_id)
        return rec.get("model", DEFAULT_MODEL) if rec else DEFAULT_MODEL

    def get_system_prompt(self, user_id: int) -> str:
        rec = self._data.get(user_id)
        return rec.get("system_prompt", "") if rec else ""

    def get_model_id(self, user_id: int) -> str:
        return MODEL_MAP.get(self.get_model(user_id), MODEL_MAP[DEFAULT_MODEL])

    def use_thinking(self, user_id: int) -> bool:
        if not self._thinking_enabled:
            return False
        return THINKING_PARAM_MAP.get(self.get_model(user_id), False)

    def is_thinking_enabled(self) -> bool:
        return self._thinking_enabled

    async def set_thinking_enabled(self, flag: bool) -> None:
        self._thinking_enabled = bool(flag)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, database.upsert, SETTINGS_TABLE, THINKING_KEY, bool(flag))
            logger.info("Думающая модель глобально: %s", "ВКЛ" if flag else "ВЫКЛ")
        except Exception as e:
            logger.error("Не удалось сохранить флаг thinking_enabled: %s", e)

    async def set_model(self, user_id: int, model_key: str) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            rec["model"] = model_key
            await self._save_to_db()

    async def set_system_prompt(self, user_id: int, prompt: str) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            rec["system_prompt"] = prompt
            await self._save_to_db()

    async def _save_to_db(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_db)
        except Exception as e:
            logger.error("Не удалось сохранить настройки в БД: %s", e)

    def _write_db(self) -> None:
        database.save_table(TABLE, {str(uid): rec for uid, rec in self._data.items()})


user_settings = UserSettings()
