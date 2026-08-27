import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, UpdateType

import config
import database
from handlers import admin, user
from user_storage import user_storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    logger.info("Бот: @%s (id=%d)", me.username, me.id)
    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def log_all_updates(handler, event, data):
        try:
            from aiogram.types import Message, CallbackQuery, InlineQuery, ChosenInlineResult
            if isinstance(event, Message):
                chat_type = getattr(event.chat, "type", "?")
                logger.info("RX-MSG type=%s chat=%s from=%s text=%r",
                            chat_type, getattr(event.chat, "id", "?"),
                            getattr(event.from_user, "id", "?"),
                            (event.text or event.caption or "")[:60])
            elif isinstance(event, CallbackQuery):
                logger.info("RX-CB from=%s data=%r", getattr(event.from_user, "id", "?"), (event.data or "")[:60])
            elif isinstance(event, InlineQuery):
                logger.info("RX-INLINE from=%s query=%r", getattr(event.from_user, "id", "?"), (event.query or "")[:60])
            elif isinstance(event, ChosenInlineResult):
                logger.info("RX-CHOSEN from=%s result=%r", getattr(event.from_user, "id", "?"), (event.result_id or "")[:60])
            else:
                logger.info("RX-OTHER type=%s", type(event).__name__)
        except Exception:
            pass
        return await handler(event, data)

    dp.include_router(admin.router)
    dp.include_router(user.router)

    # Маркер версии кода — чтобы в логе на сервере было однозначно видно,
    # какая сборка реально запущена (и доехал ли деплой).
    logger.info("CODE-VERSION v20260827-diag-PM")

    allowed = [
        UpdateType.MESSAGE,
        UpdateType.CALLBACK_QUERY,
        UpdateType.INLINE_QUERY,
        UpdateType.CHOSEN_INLINE_RESULT,
        UpdateType.GUEST_MESSAGE,
    ]

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен. Allowed updates: %s", [u.value for u in allowed])
    logger.info("База данных (SQLite): %s", config.DB_FILE_PATH)
    logger.info("Диагностика БД: %s", database.diagnose())
    logger.info("%s", database.self_test())
    logger.info("Пользователей в базе при старте: %d", user_storage.count())
    logger.info(
        "ВНИМАНИЕ: Для inline ОБЯЗАТЕЛЬНО включите inline feedback в @BotFather: "
        "/setinlinefeedback → Enable → 100%%"
    )
    if not config.TAVILY_API_KEY:
        logger.warning(
            "ВНИМАНИЕ: TAVILY_API_KEY не задан! Веб-поиск будет использовать "
            "DuckDuckGo/Википедию, которые часто rate-limit серверы. "
            "Добавьте TAVILY_API_KEY в .env для надёжного поиска."
        )

    await dp.start_polling(bot, allowed_updates=allowed)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    except Exception as e:
        logging.getLogger(__name__).critical("Критическая ошибка при запуске бота: %s", e)
        raise
