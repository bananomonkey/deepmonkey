import asyncio
import logging
import os

import config

logger = logging.getLogger(__name__)


class ModelManager:
    """Хранит текущее имя модели DeepSeek, персистит в файл — аналог PromptManager."""

    def __init__(self, file_path: str, default_model: str):
        self.file_path = file_path
        self.default_model = default_model
        self._current_model = default_model
        self._lock = asyncio.Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    self._current_model = content
                    logger.info("Модель DeepSeek загружена из файла: %s", content)
            except Exception as e:
                logger.error("Не удалось прочитать файл модели (%s): %s", self.file_path, e)
        else:
            logger.info("Файл модели не найден, используется модель по умолчанию: %s", self.default_model)

    def get(self) -> str:
        return self._current_model

    async def set(self, new_model: str) -> None:
        async with self._lock:
            self._current_model = new_model
            await self._save_to_disk(new_model)

    async def reset(self) -> None:
        await self.set(self.default_model)

    async def _save_to_disk(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_file, text)
        except Exception as e:
            logger.error("Не удалось сохранить файл модели (%s): %s", self.file_path, e)

    def _write_file(self, text: str) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write(text)


model_manager = ModelManager(config.MODEL_FILE_PATH, config.DEEPSEEK_MODEL)
