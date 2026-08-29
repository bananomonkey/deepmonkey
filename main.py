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
            from aiogram.types import Message, CallbackQuery, InlineQuery, ChosenInlineResult, Update
            upd = event if isinstance(event, Update) else None
            m = getattr(event, "message", None) if not isinstance(event, Message) else event
            if isinstance(event, Message):
                m = event
            if m is not None:
                chat_type = getattr(m.chat, "type", "?") if m.chat else "?"
                chat_id = getattr(m.chat, "id", "?") if m.chat else "?"
                is_guest = getattr(m, "guest_query_id", None)
                fu = getattr(m, "from_user", None)
                uid = getattr(fu, "id", "?")
                uname = getattr(fu, "username", "") or ""
                fname = getattr(fu, "full_name", "") or ""
                logger.info("RX-TYPE msg%s type=%s chat=%s from=%s@%s (%s) text=%r",
                            "-guest" if is_guest else "",
                            chat_type, chat_id,
                            uname or uid, uid, fname,
                            (m.text or m.caption or "")[:60])
            elif isinstance(event, CallbackQuery):
                logger.info("RX-CB from=%s data=%r", getattr(event.from_user, "id", "?"), (event.data or "")[:60])
            elif isinstance(event, InlineQuery):
                logger.info("RX-INLINE from=%s query=%r", getattr(event.from_user, "id", "?"), (event.query or "")[:60])
            elif isinstance(event, ChosenInlineResult):
                logger.info("RX-CHOSEN from=%s result=%r", getattr(event.from_user, "id", "?"), (event.result_id or "")[:60])
            else:
                t = str(type(event).__name__)
                inner = []
                for attr in ("message", "callback_query", "inline_query", "chosen_inline_result", "guest_message"):
                    if getattr(event, attr, None) is not None:
                        inner.append(attr)
                logger.info("RX-OTHER type=%s inner=%s", t, ",".join(inner) or "-")
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
    ]

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен. Allowed updates: %s", [u.value for u in allowed])
    logger.info(
        "РЕЖИМ: inline/Guest Mode отключён. Пинг @имя_бота в группе -> обычный "
        "реплай в чат; ответы на реплаи к сообщениям бота -> продолжение диалога."
    )
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

    # Автоимпорт файлов экспорта чатов из IMPORT_EXPORT_DIR (например /app/data).
    # Держит базу портретов актуальной без ручного запуска /loadchat.
    try:
        from chat_import import import_export_dir
        summary = await asyncio.to_thread(import_export_dir, config.IMPORT_EXPORT_DIR)
        if summary["imported"]:
            logger.info("Автоимпорт экспортов: импортировано %d файлов", summary["imported"])
            for fname, info in summary["files"].items():
                logger.info("  • %s → чат %s (%d сообщений)", fname, info["chat_id"], info["messages"])
        if summary["errors"]:
            logger.warning("Автоимпорт: %s", "; ".join(summary["errors"]))
    except Exception as e:
        logger.warning("Автоимпорт экспортов не выполнился: %s", e)

    # Строим базу знаний об участниках (портреты активных + кликухи чатов).
    # В фоне, чтобы не задерживать старт; пересобирает только новые/устаревшие.
    try:
        import member_knowledge

        async def warm_up_knowledge():
            try:
                await member_knowledge.build_member_knowledge()
            finally:
                # Тёплый индекс поиска по экспорту (чтобы первый запрос не тормозил).
                try:
                    await asyncio.to_thread(member_knowledge._build_search_index, True)
                except Exception as e:
                    logger.warning("Не удалось построить индекс поиска: %s", e)

        asyncio.create_task(warm_up_knowledge())
        logger.info("Запущено фоновое построение базы знаний об участниках")
    except Exception as e:
        logger.warning("Не удалось запустить построение базы знаний: %s", e)

    await dp.start_polling(bot, allowed_updates=allowed)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    except Exception as e:
        logging.getLogger(__name__).critical("Критическая ошибка при запуске бота: %s", e)
        raise
