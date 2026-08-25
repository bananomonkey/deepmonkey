import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
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
from deepseek_client import ask_deepseek_with_search, summarize_profile
from keyboards import (
    chats_list_kb,
    inline_placeholder_kb,
    model_select_kb,
    user_prompt_kb,
    user_prompt_cancel_kb,
)
from md_format import markdown_to_html
from prompt_manager import build_full_system_prompt
from rate_limiter import rate_limiter
from reply_context_store import reply_context_store
from states import UserStates
from user_settings import user_settings
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
    "/clearchat — очистить историю текущего чата\n"
    "/model — выбрать модель (⚡ быстрая / 🧠 думающая)\n"
    "/prompt — настроить свой системный промпт\n\n"
    "Я также работаю в инлайн-режиме: набери в любом чате\n"
    "<code>@имя_бота твой вопрос</code>\n"
    "и отправь получившийся результат — я отвечу прямо в этом чате, "
    "а если кто-то ответит на мой ответ — я продолжу разговор с учётом контекста.\n\n"
    "🔍 Я автоматически ищу в интернете, если ваш запрос требует актуальных данных."
)


async def _update_profile_background(user_id: int) -> None:
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
        await callback.answer("⚠️ Нужно оставить минимум 2 чата.", show_alert=True)
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


# ============================================================
#  Модель — /model
# ============================================================

@router.message(Command("model"), F.chat.type == "private")
async def cmd_model(message: Message) -> None:
    current = user_settings.get_model(message.from_user.id)
    label = "⚡ Быстрая" if current == "fast" else "🧠 Думающая"
    await message.answer(
        f"🤖 Текущая модель: <b>{label}</b>\n\nВыберите:",
        reply_markup=model_select_kb(current),
    )


@router.callback_query(F.data.startswith("set_model:"))
async def cb_set_model(callback: CallbackQuery) -> None:
    model_key = callback.data.split(":", 1)[1]
    if model_key not in ("fast", "thinking"):
        await callback.answer("Неизвестная модель", show_alert=True)
        return
    await user_settings.set_model(callback.from_user.id, model_key)
    label = "⚡ Быстрая" if model_key == "fast" else "🧠 Думающая"
    await callback.answer(f"Модель изменена на {label}")
    try:
        await callback.message.edit_reply_markup(reply_markup=model_select_kb(model_key))
    except TelegramBadRequest:
        pass


# ============================================================
#  Пользовательский промпт — /prompt
# ============================================================

@router.message(Command("prompt"), F.chat.type == "private")
async def cmd_prompt(message: Message) -> None:
    current = user_settings.get_system_prompt(message.from_user.id)
    text = "📝 Ваш системный промпт:\n\n"
    text += f"<code>{current}</code>" if current else "<i>не задан (используется промпт по умолчанию)</i>"
    text += "\n\nВы можете задать свой промпт — бот будет учитывать его поверх основного."
    await message.answer(text, reply_markup=user_prompt_kb())


@router.callback_query(F.data == "user_show_prompt")
async def cb_user_show_prompt(callback: CallbackQuery) -> None:
    current = user_settings.get_system_prompt(callback.from_user.id)
    text = "📝 Ваш системный промпт:\n\n"
    text += f"<code>{current}</code>" if current else "<i>не задан</i>"
    await callback.answer(text, show_alert=True)


@router.callback_query(F.data == "user_edit_prompt")
async def cb_user_edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.waiting_for_user_prompt)
    await callback.message.answer(
        "✏️ Отправьте свой системный промпт одним сообщением.\n"
        "Бот будет учитывать его поверх основного системного промпта.",
        reply_markup=user_prompt_cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "user_reset_prompt")
async def cb_user_reset_prompt(callback: CallbackQuery) -> None:
    await user_settings.set_system_prompt(callback.from_user.id, "")
    await callback.answer("✅ Промпт сброшен", show_alert=True)


@router.callback_query(F.data == "user_prompt_cancel")
async def cb_user_prompt_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")


@router.message(UserStates.waiting_for_user_prompt, F.text)
async def cb_user_edit_prompt_finish(message: Message, state: FSMContext) -> None:
    new_prompt = message.text.strip()
    if not new_prompt:
        await message.answer("⚠️ Промпт не может быть пустым.", reply_markup=user_prompt_cancel_kb())
        return
    await user_settings.set_system_prompt(message.from_user.id, new_prompt)
    await state.clear()
    await message.answer("✅ Ваш системный промпт сохранён!", reply_markup=user_prompt_kb())


@router.message(UserStates.waiting_for_user_prompt)
async def cb_user_edit_prompt_wrong_type(message: Message) -> None:
    await message.answer(
        "⚠️ Пришлите промпт текстовым сообщением.",
        reply_markup=user_prompt_cancel_kb(),
    )


@router.message(F.text, F.chat.type == "private")
async def handle_text(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    if user_storage.is_banned(user_id):
        return

    if not await rate_limiter.allow(user_id):
        await message.answer("⏳ Слишком много сообщений подряд. Подождите немного и попробуйте снова.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = build_full_system_prompt(profile, user_custom_prompt)
    history = chat_sessions.get_history(user_id)

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, message.text, history=history,
            model=model_id, use_thinking=use_thinking,
        )
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
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = build_full_system_prompt(profile, user_custom_prompt)

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, message.text, history=history,
            model=model_id, use_thinking=use_thinking,
        )
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
#  Инлайн-режим — typewriter-анимация
# ============================================================

INLINE_CHUNK_SIZE = 8
INLINE_EDIT_DELAY = 0.18
INLINE_INITIAL_DELAY = 0.5


@router.inline_query()
async def handle_inline(inline_query: InlineQuery) -> None:
    query_text = inline_query.query.strip()

    if not query_text:
        await inline_query.answer([], cache_time=1)
        return

    if user_storage.is_banned(inline_query.from_user.id):
        await inline_query.answer([], cache_time=1)
        return

    results = [
        InlineQueryResultArticle(
            id="ask",
            title="Спросить ИИ",
            description=query_text[:100],
            input_message_content=InputTextMessageContent(message_text=f"❓ {query_text}"),
            reply_markup=inline_placeholder_kb(),
        )
    ]
    await inline_query.answer(results, cache_time=1, is_personal=True)


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot) -> None:
    inline_message_id = chosen.inline_message_id
    user_id = chosen.from_user.id
    query_text = chosen.query.strip()

    logger.info(
        "INLINE chosen: user=%s query=%s inline_message_id=%s",
        user_id, query_text[:80], inline_message_id,
    )

    if not inline_message_id:
        logger.warning("INLINE: нет inline_message_id для user=%s — невозможно редактировать", user_id)
        return

    if user_storage.is_banned(user_id):
        try:
            await bot.edit_message_text(
                "🚫 Вы заблокированы.", inline_message_id=inline_message_id,
            )
        except Exception as e:
            logger.error("INLINE edit failed (banned): %s", e)
        return

    if not await rate_limiter.allow(user_id):
        try:
            await bot.edit_message_text(
                "⏳ Слишком много запросов. Подождите.", inline_message_id=inline_message_id,
            )
        except Exception as e:
            logger.error("INLINE edit failed (rate limit): %s", e)
        return

    await user_storage.touch(user_id, chosen.from_user.username, chosen.from_user.full_name)

    try:
        await bot.edit_message_text("⏳ Думаю...", inline_message_id=inline_message_id)
    except Exception as e:
        logger.error("INLINE edit failed (initial 'Думаю'): %s", e)

    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = build_full_system_prompt(profile, user_custom_prompt)

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, query_text, model=model_id, use_thinking=use_thinking,
        )
    except Exception as e:
        logger.error("INLINE ошибка DeepSeek: %s", e)
        answer = "⚠️ Ошибка при обращении к нейросети."
        try:
            await bot.edit_message_text(answer, inline_message_id=inline_message_id)
        except Exception as e2:
            logger.error("INLINE edit failed (error msg): %s", e2)
        return

    html_answer = markdown_to_html(answer)
    plain_answer = answer

    displayed = ""
    i = 0
    while i < len(plain_answer):
        chunk_end = min(i + INLINE_CHUNK_SIZE, len(plain_answer))
        displayed = plain_answer[:chunk_end]
        i = chunk_end

        edit_text = markdown_to_html(displayed)
        try:
            await bot.edit_message_text(
                edit_text, inline_message_id=inline_message_id,
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                logger.error("INLINE typewriter edit error: %s", e)
        except Exception as e:
            logger.error("INLINE typewriter unexpected error: %s", e)
            break

        await asyncio.sleep(INLINE_EDIT_DELAY)

    final_html = markdown_to_html(answer)
    try:
        await bot.edit_message_text(
            final_html, inline_message_id=inline_message_id,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.error("INLINE final edit as HTML failed (%s), trying plain text", e)
            try:
                await bot.edit_message_text(
                    plain_answer, inline_message_id=inline_message_id, parse_mode=None,
                )
            except Exception as e2:
                logger.error("INLINE final edit plain text failed: %s", e2)
    except Exception as e:
        logger.error("INLINE final edit failed: %s", e)

    history = [{"role": "user", "content": query_text}, {"role": "assistant", "content": answer}]
    await reply_context_store.save_context(answer, history, user_id)
