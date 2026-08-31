import asyncio
import logging

import config
import database

logger = logging.getLogger(__name__)

TABLE = "settings"
KEY = "prompt"

# Старые дефолтные промпты (содержали роль/объём ответа) — подлежат миграции на новый.
_LEGACY_DEFAULT_PROMPTS = [
    "Ты — полезный, дружелюбный ассистент. "
    "Отвечай кратко, по существу и на языке пользователя.",
    "Отвечай кратко и по существу. Всегда отвечай на языке пользователя.",
]


class PromptManager:
    """
    Управляет системным промтом:
    - хранит его в памяти для быстрого доступа;
    - persist'ит в SQLite, чтобы промт не терялся при деплое.
    """

    def __init__(self, default_prompt: str):
        self.default_prompt = default_prompt
        self._current_prompt = default_prompt
        self._lock = asyncio.Lock()
        self._load_from_db()
        self._migrate_legacy_default()

    def _load_from_db(self) -> None:
        try:
            value = database.get_value(TABLE, KEY)
            if value:
                self._current_prompt = value
                logger.info("Системный промт загружен из БД")
            else:
                logger.info("Промт в БД не найден, используется промт по умолчанию.")
        except Exception as e:
            logger.error("Не удалось прочитать промт из БД: %s", e)

    def _migrate_legacy_default(self) -> None:
        """Если в БД лежит один из старых дефолтных промптов (с ролью или объёмом ответа),
        заменяем его на новый (только язык, без роли и ограничений объёма)."""
        if self._current_prompt.strip() in _LEGACY_DEFAULT_PROMPTS:
            self._current_prompt = self.default_prompt
            try:
                database.upsert(TABLE, KEY, self.default_prompt)
                logger.info("Мигрирован старый дефолтный промпт на новый (только язык)")
            except Exception as e:
                logger.error("Не удалось сохранить мигрированный промпт в БД: %s", e)

    def get(self) -> str:
        """Синхронное чтение текущего промта (безопасно, т.к. запись атомарна на уровне Python)."""
        return self._current_prompt

    async def set(self, new_prompt: str) -> None:
        async with self._lock:
            self._current_prompt = new_prompt
            await self._save_to_db(new_prompt)

    async def reset(self) -> None:
        await self.set(self.default_prompt)

    async def _save_to_db(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, database.upsert, TABLE, KEY, text)
            logger.info("Системный промт сохранён в БД")
        except Exception as e:
            logger.error("Не удалось сохранить промт в БД: %s", e)


# Единый инстанс на всё приложение
prompt_manager = PromptManager(config.DEFAULT_SYSTEM_PROMPT)


def build_full_system_prompt(profile: str = "", user_custom_prompt: str = "") -> str:
    """
    Собирает ЕДИНЫЙ системный промт, который уходит в нейросеть.

    Чтобы бот отвечал хорошо, промт не раздуваем наслоениями (профили, знания,
    множественные инструкции). Используется один блок роли + короткий guard:
      - пользовательский промт (если задан) — единственный промт роли;
      - иначе — глобальный промт бота (заданный админом / по умолчанию);
      - профиль и прочие вспомогательные заметки в системный промт НЕ включаются.
    """
    role = (user_custom_prompt or "").strip() or (prompt_manager.get() or "").strip()
    parts = [config.GUARD_SYSTEM_PROMPT.strip()]
    if role:
        parts.append(role)
    return "\n\n".join(p for p in parts if p)
