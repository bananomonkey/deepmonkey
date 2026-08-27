# -*- coding: utf-8 -*-
"""
userbot_export.py — разовый экспорт истории группового чата через юзербота.

Зачем: Telegram Bot API не даёт ботам историю группы (до вступления).
Личный Telegram-аккаунт (через Telethon) — даёт. Этот скрипт подключается
к уже готовой Telethon-сессии личного аккаунта и выгружает переписку
(текст) в общую SQLite (таблица member_log_all), чтобы бот мог строить
портреты участников из ПОЛНОЙ истории.

Запуск (примеры):
    python userbot_export.py --chat <chat_id|@username> --limit 2000
    python userbot_export.py --chat <chat_id> --user <user_id|@username> --limit 500

Примечания:
- Запускается ВРУЧНУЮ (не постоянно). После экспорта процесс завершается.
- Читаем только текст (без медиа) — этого достаточно для портретов.
- Паузы между батчами защищают аккаунт от бана за агрессивное чтение.
- Только для чатов, где юзербот-аккаунт является участником.

Требуемые переменные env: USERBOT_API_ID, USERBOT_API_HASH,
USERBOT_SESSION_PATH (см. config.py). Без них скрипт не запустится.
"""

import argparse
import asyncio
import logging
import sys
import time

import config
import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("userbot_export")


async def get_client():
    from telethon import TelegramClient

    if not config.USERBOT_API_ID or not config.USERBOT_API_HASH:
        raise SystemExit(
            "Юзербот не настроен: задайте USERBOT_API_ID и USERBOT_API_HASH "
            "(и, при необходимости, USERBOT_SESSION_PATH) в .env."
        )
    client = TelegramClient(
        config.USERBOT_SESSION_PATH,
        config.USERBOT_API_ID,
        config.USERBOT_API_HASH,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(
            f"Сессия {config.USERBOT_SESSION_PATH!r} не авторизована. "
            "Сначала авторизуйте аккаунт."
        )
    return client


async def run(args) -> None:
    client = await get_client()
    try:
        me = await client.get_me()
        logger.info("Подключено как %s (%s)", me.first_name, me.username or me.id)

        entity = await client.get_entity(args.chat)
        chat_id = getattr(entity, "id", entity)
        logger.info(
            "Чат: %s (id=%s, %s)", getattr(entity, "title", entity), chat_id, entity.__class__.__name__
        )

        target_user_id = None
        target_user_label = None
        if args.user:
            try:
                user_id_int = int(str(args.user))
                target_user_id = user_id_int
                target_user_label = str(user_id_int)
            except ValueError:
                user_entity = await client.get_entity(args.user)
                target_user_id = getattr(user_entity, "id", None)
                target_user_label = str(args.user)
            logger.info("Фильтр по участнику: %s (id=%s)", target_user_label, target_user_id)

        collected = []
        count = 0
        batch_buffer = []
        delay = config.USERBOT_EXPORT_DELAY
        batch_size = args.batch or config.USERBOT_EXPORT_BATCH

        async for msg in client.iter_messages(
            entity,
            limit=args.limit,
            wait_time=delay / 2,
            reverse=False,
        ):
            if msg is None:
                continue
            raw = getattr(msg, "raw_text", "") or ""
            sender = getattr(msg, "sender_id", None)
            if not raw.strip():
                continue
            if getattr(msg, "service", False):
                continue

            if target_user_id is not None and sender != target_user_id:
                continue

            username = None
            name = None
            if msg.sender is not None:
                username = getattr(msg.sender, "username", None)
                name = (
                    getattr(msg.sender, "first_name", None)
                    or getattr(msg.sender, "last_name", None)
                    or username
                )

            batch_buffer.append({
                "user_id": str(sender) if sender is not None else None,
                "username": username,
                "name": name,
                "text": raw[:4000],
                "date": (getattr(msg, "date", None) or "").isoformat()
                if getattr(msg, "date", None)
                else "",
            })
            count += 1

            if count % 1000 == 0:
                logger.info("Собрано сообщений: %d", count)

            if len(batch_buffer) >= batch_size:
                collected = batch_buffer + collected
                batch_buffer = []
                logger.info("Промежуточно сохранено: %d (всего в памяти %d)", len(collected), count)
                await asyncio.sleep(delay)

        if batch_buffer:
            collected = batch_buffer + collected

        # dedupe по (user_id, date, text) и сортировка по дате
        seen = set()
        deduped = []
        for m in reversed(collected):
            key = (m.get("user_id"), m.get("date"), m.get("text"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)

        database.save_exported_messages(chat_id, deduped)
        logger.info(
            "ГОТОВО: %d уникальных текстовых сообщений сохранено в БД "
            "для чата %s" % (len(deduped), chat_id)
        )
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт истории чата через юзербота")
    parser.add_argument("--chat", required=True, help="Чат: id или @username")
    parser.add_argument("--user", default=None, help="Фильтр: user_id или @username (необязательно)")
    parser.add_argument("--limit", type=int, default=2000, help="Макс. число сообщений (всего)")
    parser.add_argument("--batch", type=int, default=0, help="Размер блока чтения (переопределяет env)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
