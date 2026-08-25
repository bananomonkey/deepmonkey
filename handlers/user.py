import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

import config
from chat_sessions import chat_sessions
from deepseek_client import ask_deepseek, summarize_profile
from keyboards import chats_list_kb, inline_placeholder_kb
from md_format import markdown_to_html
from prompt_manager import build_full_system_prompt
from rate_limiter import rate_limiter
from reply_context_store import reply_context_store
from user_storage import user_storage

logger = logging.getLogger(__name__)
router = Router(name="user")

WELCOME_TEXT = (
    "👋 Привет! Я бот на базе ИИ.\n\n"
    "Просто напиши мне сообщение — и я отвечу, помня контекст нашего разговора.\n\n"
    "Команды:\n"
    "/start — это сообщение\n"
    "/newchat — начать новый чат с чистого листа\n"
    "/chats — список ваших чатов (переключение и удаление)\n"
    "/clearchat — очистить историю текущего чата\n\n"
    "Я также работаю в инлайн-режиме: набери в любом чате\n"
    "<code>@имя_бота твой вопрос</code>\n"
    "и отправь получившийся результат — я отвечу прямо в этом чате, "
    "а если кто-то ответит на мой ответ — я продолжу разговор с учётом контекста."
)


async def _update_profile_background(user_id: int) -> None:
    """Фоновое обновление краткой заметки об интересах пользователя (не блокирует ответ)."""
    try:
        history = chat_sessions.get_history(user_id)
        if not history:
            return
        old_profile = user_storage.get_profile(user_id)
        new_profile = await summarize_profile(old_profile, history)
        await user_storage.set_profile(user_id, new_profile)
        logger.info("Профиль пользователя %s обновлён", user_id)
    except Exception as e:
        logger.error("Ошибка фонового обновления профиля пользователя %s: %s", user_id, e)


async def _safe_answer(message: Message, text_markdown: str) -> None:
    """Отправляет ответ, конвертируя Markdown в HTML; при сбое парсинга — обычным текстом."""
    html_text = markdown_to_html(text_markdown)
    try:
        await message.answer(html_text)
    except TelegramBadRequest as e:
        logger.error("Не удалось отправить как HTML (%s), отправляю как обычный текст", e)
        await message.answer(text_markdown, parse_mode=None)


# ============================================================
#  Обычный режим (личные сообщения)
# ============================================================

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message) -> None:
    await user_storage.touch(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(WELCOME_TEXT)


@router.message(Command("newchat"), F.chat.type == "private")
async def cmd_newchat(message: Message) -> None:
    user_id = message.from_user.id
    if user_storage.is_banned(user_id):
        return
    chat_id = await chat_sessions.create_chat(user_id)
    if chat_id is None:
        await message.answer(
            f"⚠️ Достигнут лимит чатов ({config.MAX_CHATS_PER_USER}). "
            "Удалите один из существующих через /chats, чтобы создать новый."
        )
        return
    await message.answer("🆕 Новый чат создан и открыт, история чистая. Пишите!")


@router.message(Command("chats"), F.chat.type == "private")
async def cmd_chats(message: Message) -> None:
    await chat_sessions.ensure_user(message.from_user.id)
    chats = chat_sessions.list_chats(message.from_user.id)
    await message.answer("Ваши чаты (✅ — текущий):", reply_markup=chats_list_kb(chats))


@router.message(Command("clearchat"), F.chat.type == "private")
async def cmd_clearchat(message: Message) -> None:
    await chat_sessions.clear_active_chat(message.from_user.id)
    await message.answer("🧹 История текущего чата очищена.")


@router.callback_query(F.data.startswith("chat_switch:"))
async def cb_chat_switch(callback: CallbackQuery) -> None:
    chat_id = callback.data.split(":", 1)[1]
    ok = await chat_sessions.switch_chat(callback.from_user.id, chat_id)
    if not ok:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await callback.answer("Переключено")
    chats = chat_sessions.list_chats(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(reply_markup=chats_list_kb(chats))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("chat_delete:"))
async def cb_chat_delete(callback: CallbackQuery) -> None:
    chat_id = callback.data.split(":", 1)[1]
    ok = await chat_sessions.delete_chat(callback.from_user.id, chat_id)
    if not ok:
        await callback.answer("Чат не найден", show_alert=True)
        return
    await callback.answer("Удалено")
    chats = chat_sessions.list_chats(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(reply_markup=chats_list_kb(chats))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "chat_new")
async def cb_chat_new(callback: CallbackQuery) -> None:
    chat_id = await chat_sessions.create_chat(callback.from_user.id)
    if chat_id is None:
        await callback.answer(f"Лимит чатов: {config.MAX_CHATS_PER_USER}", show_alert=True)
        return
    await callback.answer("Создано")
    chats = chat_sessions.list_chats(callback.from_user.id)
    try:
        await callback.message.edit_reply_markup(reply_markup=chats_list_kb(chats))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text, F.chat.type == "private")
async def handle_text(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    if user_storage.is_banned(user_id):
        # Тихо игнорируем — не тратим токены и не подтверждаем сам факт бана.
        return

    if not await rate_limiter.allow(user_id):
        await message.answer("⏳ Слишком много сообщений подряд. Подождите немного и попробуйте снова.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    profile = user_storage.get_profile(user_id)
    system_prompt = build_full_system_prompt(profile)
    history = chat_sessions.get_history(user_id)

    try:
        answer = await ask_deepseek(system_prompt, message.text, history=history)
    except Exception as e:
        logger.error("Необработанная ошибка в handle_text: %s", e)
        answer = "⚠️ Произошла непредвиденная ошибка. Попробуйте позже."

    await _safe_answer(message, answer)

    await chat_sessions.append_message(user_id, "user", message.text)
    await chat_sessions.append_message(user_id, "assistant", answer)

    record = user_storage.get_record(user_id)
    if (
        record
        and config.PROFILE_UPDATE_EVERY_N_MESSAGES > 0
        and record["message_count"] % config.PROFILE_UPDATE_EVERY_N_MESSAGES == 0
    ):
        asyncio.create_task(_update_profile_background(user_id))


# ============================================================
#  Реплаи в группах (если ответили на сообщение бота)
# ============================================================

@router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message, F.text)
async def handle_group_reply(message: Message, bot: Bot) -> None:
    reply_to = message.reply_to_message
    is_reply_to_bot = bool(reply_to.from_user and reply_to.from_user.id == bot.id) or bool(
        getattr(reply_to, "via_bot", None) and reply_to.via_bot and reply_to.via_bot.id == bot.id
    )
    if not is_reply_to_bot:
        return

    user_id = message.from_user.id

    if user_storage.is_banned(user_id):
        return

    if not await rate_limiter.allow(user_id):
        await message.reply("⏳ Слишком много сообщений подряд. Подождите немного.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    context = reply_context_store.get_context(reply_to.text or reply_to.caption or "")
    history = list(context["history"]) if context else []

    profile = user_storage.get_profile(user_id)
    system_prompt = build_full_system_prompt(profile)

    try:
        answer = await ask_deepseek(system_prompt, message.text, history=history)
    except Exception as e:
        logger.error("Ошибка при ответе в группе: %s", e)
        answer = "⚠️ Произошла ошибка. Попробуйте позже."

    html_answer = markdown_to_html(answer)
    try:
        await message.reply(html_answer)
    except TelegramBadRequest:
        await message.reply(answer, parse_mode=None)

    new_history = history + [
        {"role": "user", "content": message.text},
        {"role": "assistant", "content": answer},
    ]
    max_messages = config.HISTORY_MAX_TURNS * 2
    if len(new_history) > max_messages:
        new_history = new_history[-max_messages:]

    await reply_context_store.save_context(answer, new_history, user_id)


# ============================================================
#  Инлайн-режим — отправка запроса и ответ с анимацией через редактирование
# ============================================================

@router.inline_query()
async def handle_inline(inline_query: InlineQuery) -> None:
    query_text = inline_query.query.strip()

    if not query_text:
        await inline_query.answer([], cache_time=1)
        return

    if user_storage.is_banned(inline_query.from_user.id):
        await inline_query.answer([], cache_time=1)
        return

    # Реальный запрос к DeepSeek НЕ делаем здесь — только когда пользователь
    # действительно отправит выбранный результат (см. handle_chosen_inline_result).
    # Так inline-подсказка появляется мгновенно и не тратит токены на каждую букву.
    results = [
        InlineQueryResultArticle(
            id="ask",
            title="Спросить ИИ",
            description=query_text[:100],
            input_message_content=InputTextMessageContent(message_text=f"❓ {query_text}"),
            reply_markup=inline_placeholder_kb(),
        )
    ]
    await inline_query.answer(results, cache_time=300, is_personal=True)


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot) -> None:
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        # Без этого поля редактировать сообщение невозможно — см. README:
        # нужно включить инлайн-фидбек через @BotFather (/setinlinefeedback -> 100%).
        return

    user_id = chosen.from_user.id
    query_text = chosen.query.strip()

    if user_storage.is_banned(user_id):
        try:
            await bot.edit_message_text("🚫 Вы заблокированы и не можете использовать бота.", inline_message_id=inline_message_id)
        except Exception:
            pass
        return

    if not await rate_limiter.allow(user_id):
        try:
            await bot.edit_message_text("⏳ Слишком много запросов подряд. Подождите немного.", inline_message_id=inline_message_id)
        except Exception:
            pass
        return

    await user_storage.touch(user_id, chosen.from_user.username, chosen.from_user.full_name)

    stop_typing = asyncio.Event()

    async def _animate() -> None:
        frames = ["⏳ Думаю", "⏳ Думаю.", "⏳ Думаю..", "⏳ Думаю..."]
        i = 0
        try:
            while not stop_typing.is_set():
                try:
                    await bot.edit_message_text(frames[i % len(frames)], inline_message_id=inline_message_id)
                except Exception:
                    pass  # "message is not modified" / rate limit на edit — не критично, просто пропускаем кадр
                i += 1
                await asyncio.sleep(1.2)
        except asyncio.CancelledError:
            pass

    anim_task = asyncio.create_task(_animate())

    profile = user_storage.get_profile(user_id)
    system_prompt = build_full_system_prompt(profile)

    try:
        answer = await ask_deepseek(system_prompt, query_text)
    except Exception as e:
        logger.error("Ошибка в inline-режиме: %s", e)
        answer = "⚠️ Ошибка при обращении к нейросети."
    finally:
        stop_typing.set()
        anim_task.cancel()

    html_answer = markdown_to_html(answer)
    try:
        await bot.edit_message_text(html_answer, inline_message_id=inline_message_id)
    except TelegramBadRequest as e:
        logger.error("Не удалось отредактировать inline-сообщение как HTML (%s), пробую как текст", e)
        try:
            await bot.edit_message_text(answer, inline_message_id=inline_message_id, parse_mode=None)
        except Exception as e2:
            logger.error("Не удалось отредактировать inline-сообщение: %s", e2)
    except Exception as e:
        logger.error("Не удалось отредактировать inline-сообщение: %s", e)

    history = [{"role": "user", "content": query_text}, {"role": "assistant", "content": answer}]
    await reply_context_store.save_context(answer, history, user_id)
