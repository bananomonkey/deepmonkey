import asyncio
import logging

import config
import database

logger = logging.getLogger(__name__)

TABLE = "settings"
KEY = "model"


class ModelManager:
    """Хранит текущее имя модели LLM (Google Gemini), персистит в SQLite."""

    def __init__(self, default_model: str):
        self.default_model = default_model
        self._current_model = default_model
        self._lock = asyncio.Lock()
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            value = database.get_value(TABLE, KEY)
            if value:
                self._current_model = value
                logger.info("Модель LLM загружена из БД: %s", value)
            else:
                logger.info("Модель в БД не найдена, используется по умолчанию: %s", self.default_model)
        except Exception as e:
            logger.error("Не удалось прочитать модель из БД: %s", e)

    def get(self) -> str:
        return self._current_model

    async def set(self, new_model: str) -> None:
        async with self._lock:
            self._current_model = new_model
            await self._save_to_db(new_model)

    async def reset(self) -> None:
        await self.set(self.default_model)

    async def _save_to_db(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, database.upsert, TABLE, KEY, text)
        except Exception as e:
            logger.error("Не удалось сохранить модель в БД: %s", e)


model_manager = ModelManager(config.GEMINI_MODEL)
