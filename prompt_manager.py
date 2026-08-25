import asyncio
import logging
import os

import config

logger = logging.getLogger(__name__)


class PromptManager:
    """
    Управляет системным промтом:
    - хранит его в памяти для быстрого доступа;
    - persist'ит на диск в текстовый файл, чтобы промт не терялся при рестарте
      (актуально для Bothost, где нет SSH и БД может быть не подключена).
    """

    def __init__(self, file_path: str, default_prompt: str):
        self.file_path = file_path
        self.default_prompt = default_prompt
        self._current_prompt = default_prompt
        self._lock = asyncio.Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    self._current_prompt = content
                    logger.info("Системный промт загружен из файла: %s", self.file_path)
                else:
                    logger.warning("Файл промта пуст, используется промт по умолчанию.")
            except Exception as e:
                logger.error("Не удалось прочитать файл промта (%s): %s", self.file_path, e)
        else:
            logger.info("Файл промта не найден, используется промт по умолчанию.")

    def get(self) -> str:
        """Синхронное чтение текущего промта (безопасно, т.к. запись атомарна на уровне Python)."""
        return self._current_prompt

    async def set(self, new_prompt: str) -> None:
        async with self._lock:
            self._current_prompt = new_prompt
            await self._save_to_disk(new_prompt)

    async def reset(self) -> None:
        await self.set(self.default_prompt)

    async def _save_to_disk(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_file, text)
            logger.info("Системный промт сохранён в файл: %s", self.file_path)
        except Exception as e:
            # Не роняем бота, если запись на диск не удалась (например, read-only fs) —
            # просто логируем, промт всё равно останется актуальным в памяти до рестарта.
            logger.error("Не удалось сохранить файл промта (%s): %s", self.file_path, e)

    def _write_file(self, text: str) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write(text)


# Единый инстанс на всё приложение
prompt_manager = PromptManager(config.PROMPT_FILE_PATH, config.DEFAULT_SYSTEM_PROMPT)


def build_full_system_prompt(profile: str = "") -> str:
    """
    Собирает итоговый системный промт, который реально уходит в DeepSeek:
    1) неизменяемая guard-инструкция (защита личности бота и промта от раскрытия/джейлбрейков);
    2) редактируемая часть, которую меняет админ через /admin;
    3) при наличии — краткая заметка об интересах пользователя (для персонализации).
    """
    parts = [config.GUARD_SYSTEM_PROMPT.strip(), prompt_manager.get().strip()]
    if profile:
        parts.append(
            "Вот что уже известно о пользователе (используй только как контекст "
            "для более персонального ответа; не зачитывай эту заметку "
            "пользователю дословно и не упоминай, что она существует):\n"
            + profile.strip()
        )
    return "\n\n".join(p for p in parts if p)
