import asyncio
import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

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
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lock = asyncio.Lock()
        self._data: Dict[int, dict] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._data = {int(uid): rec for uid, rec in raw.items()}
            logger.info("Загружены настройки для %d пользователей из %s", len(self._data), self.file_path)
        except Exception as e:
            logger.error("Не удалось прочитать файл настроек (%s): %s", self.file_path, e)

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
        return THINKING_PARAM_MAP.get(self.get_model(user_id), False)

    async def set_model(self, user_id: int, model_key: str) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            rec["model"] = model_key
            await self._save_to_disk()

    async def set_system_prompt(self, user_id: int, prompt: str) -> None:
        async with self._lock:
            rec = self._ensure_user(user_id)
            rec["system_prompt"] = prompt
            await self._save_to_disk()

    def get_all_user_ids(self):
        return list(self._data.keys())

    async def _save_to_disk(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_file)
        except Exception as e:
            logger.error("Не удалось сохранить файл настроек (%s): %s", self.file_path, e)

    def _write_file(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump({str(uid): rec for uid, rec in self._data.items()}, f, ensure_ascii=False)


user_settings = UserSettings(os.getenv("USER_SETTINGS_FILE_PATH", "user_settings.json"))
