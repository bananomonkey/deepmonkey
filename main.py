import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from handlers import admin, user

# Логи выводятся в stdout — на Bothost это единственный способ их посмотреть (SSH нет)
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
    dp = Dispatcher()

    # Порядок важен: сначала админ-роутер (более строгий фильтр),
    # затем общий пользовательский роутер.
    dp.include_router(admin.router)
    dp.include_router(user.router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот успешно запущен, начинаю polling...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
    except Exception as e:
        # Ловим всё на самом верхнем уровне, чтобы процесс не падал молча
        logging.getLogger(__name__).critical("Критическая ошибка при запуске бота: %s", e)
        raise
