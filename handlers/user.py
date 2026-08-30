import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.enums import ChatAction, MessageEntityType, ParseMode
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

import html
import re

import config
from chat_import import parse_chat_export_json
import database
from chat_sessions import chat_sessions
from deepseek_client import (
    ask_deepseek_with_search,
    describe_personality,
    summarize_member_profile,
    summarize_profile,
)
from group_sessions import group_sessions
import member_knowledge
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
from user_settings import MODEL_MAP, user_settings
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
    "и я отвечу реплаем прямо в этот чат — "
    "даже если меня нет в участниках группы. "
    "А если кто-то ответит на мой ответ — я продолжу разговор с учётом контекста.\n\n"
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


LONG_ANSWER_THRESHOLD = 700


def wrap_long_answer(html_text: str) -> str:
    """Оборачивает длинный ответ в сворачиваемое цитируемое блоку.

    Telegram Bot API поддерживает <blockquote expandable> — цитируемый блок,
    который сворачивается до одной строки (нажмите, чтобы раскрыть).
    Короткие ответы оставляем как есть, чтобы не мешать чтению.
    """
    if len(html_text) <= LONG_ANSWER_THRESHOLD:
        return html_text
    return f"<blockquote expandable>{html_text}</blockquote>"


async def _safe_answer(message: Message, text_markdown: str) -> None:
    html_text = wrap_long_answer(markdown_to_html(text_markdown))
    try:
        await message.answer(html_text)
        logger.info("Ответ отправлен в PM (HTML), длина=%d", len(text_markdown))
    except TelegramBadRequest as e:
        logger.error("Не удалось отправить как HTML (%s), отправляю как обычный текст", e)
        try:
            await message.answer(text_markdown, parse_mode=None)
            logger.info("Ответ отправлен в PM (plain) после HTML-ошибки")
        except Exception as e2:
            logger.error("Не удалось отправить даже обычным текстом: %s", e2)


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
        await callback.answer("⚠️ Нельзя удалить последний чат.", show_alert=True)
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
    label = MODEL_LABELS.get(current, "⚡ Быстрая")
    await message.answer(
        f"🤖 Текущая модель: <b>{label}</b>\n\nВыберите:",
        reply_markup=model_select_kb(current),
    )


MODEL_LABELS = {
    "fast": "⚡ Быстрая",
    "thinking": "🧠 Думающая",
    "vision": "👁 Vision (видит фото)",
}


@router.callback_query(F.data.startswith("set_model:"))
async def cb_set_model(callback: CallbackQuery) -> None:
    model_key = callback.data.split(":", 1)[1]
    if model_key not in ("fast", "thinking", "vision"):
        await callback.answer("Неизвестная модель", show_alert=True)
        return
    await user_settings.set_model(callback.from_user.id, model_key)
    label = MODEL_LABELS.get(model_key, "⚡ Быстрая")
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


@router.message(F.document, F.chat.type == "private")
async def handle_chat_export_document(message: Message) -> None:
    """Приём файла экспорта чата (JSON от плагина ChatExport) в ЛС от админа.

    Парсит JSON, сохраняет сообщения в member_log_all по chat_id. После этого
    запросы 'кто такой @X' строятся по этой истории (даже до вступления бота).
    """
    if message.from_user.id != config.ADMIN_ID:
        return

    doc = message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    if not fname.endswith((".json", ".txt")):
        await message.answer("Прикрепите файл экспорта чата в формате <b>.json</b>.")
        return

    await message.answer("⏳ Читаю файл экспорта...")

    try:
        bot_file = await message.bot.get_file(doc.file_id)
        raw = (await message.bot.download_file(bot_file.file_path)).read()
    except Exception as e:
        logger.error("Не удалось скачать файл экспорта: %s", e)
        await message.answer("⚠️ Не удалось скачать файл.")
        return

    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось декодировать файл: {e}")
        return

    try:
        parsed = parse_chat_export_json(text)
    except ValueError as e:
        await message.answer(f"⚠️ Ошибка в файле: {e}")
        return

    chat_id = parsed["chat_id"]
    msgs = parsed["messages"]
    if chat_id is None:
        await message.answer("⚠️ В JSON не найден dialog_id (id чата).")
        return

    # В экспорте плагина нет username, но он есть в live-логе группы.
    # Сопоставляем по user_id, чтобы потом находить людей по @username.
    uname_to_uid = group_sessions.get_member_username_map(chat_id)
    uid_to_uname = {str(uid): un for un, uid in uname_to_uid.items()}
    for m in msgs:
        if not m.get("username"):
            m["username"] = uid_to_uname.get(str(m.get("user_id")))

    try:
        database.save_exported_messages(chat_id, msgs)
    except Exception as e:
        logger.error("Не удалось сохранить экспорт в БД: %s", e)
        await message.answer(f"⚠️ Не удалось сохранить в базу: {e}")
        return

    matched = sum(1 for m in msgs if m.get("username"))
    await message.answer(
        f"✅ Экспорт чата <b>{parsed['chat_name'] or chat_id}</b> принят.\n"
        f"Сообщений с текстом: <b>{len(msgs)}</b>\n"
        f"Привязано по username: <b>{matched}</b>\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        f"Теперь спросите: <code>@имя_бота кто такой @юзернейм</code>"
    )


@router.message(F.photo, F.chat.type == "private")
async def handle_photo_pm(message: Message, bot: Bot) -> None:
    """Приём фото в ЛС (мультимодальность): картинка + подпись → DeepSeek Vision."""
    user_id = message.from_user.id

    if user_storage.is_banned(user_id):
        return

    if not user_settings.is_multimodal_enabled():
        await message.answer(
            "👁 Мультимодальность сейчас выключена. Включите её в админ-панели "
            "или выберите модель Vision через /model, если она доступна."
        )
        return

    if not await rate_limiter.allow(user_id):
        await message.answer("⏳ Слишком много сообщений подряд. Подождите немного.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Берём самое большое разрешение из присланных
    photo = message.photo[-1]
    try:
        data_url = await _download_image_data(bot, photo)
    except Exception as e:
        logger.error("Не удалось скачать фото: %s", e)
        await message.answer("⚠️ Не удалось обработать фото.")
        return

    caption = (message.caption or "").strip() or "Что на этом фото?"

    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = build_full_system_prompt(profile, user_custom_prompt)
    history = chat_sessions.get_history(user_id)

    # Для фото всегда используем vision-модель (не текущую текстовую),
    # и не включаем thinking (см. deepseek_client).
    model_id = MODEL_MAP["vision"]
    use_thinking = False

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, caption, history=history,
            model=model_id, use_thinking=use_thinking, images=[data_url],
        )
    except Exception as e:
        logger.error("Ошибка при ответе на фото: %s", e)
        answer = "⚠️ Произошла ошибка при анализе фото. Попробуйте позже."

    await _safe_answer(message, answer)

    await chat_sessions.append_message(user_id, "user", f"[фото] {caption}")
    await chat_sessions.append_message(user_id, "assistant", answer)


@router.message(F.text, F.chat.type == "private")
async def handle_text(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    logger.info("PM от user_id=%s username=%s name=%r: %r", user_id, message.from_user.username, message.from_user.full_name, (message.text or "")[:80])
    if user_storage.is_banned(user_id):
        logger.warning("PM игнорирован: user_id=%s забанен", user_id)
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

    # Если вопрос про людей/содержимое чата (кликухи, мемы) — ищем в экспорте.
    chat_context = await _maybe_chat_context(message.text)

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, message.text, history=history,
            model=model_id, use_thinking=use_thinking,
            chat_context=chat_context,
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
#  Группы: ответ на сообщение бота ИЛИ упоминание @bot
# ============================================================

def _is_reply_to_bot(message: Message, bot: Bot) -> bool:
    reply_to = message.reply_to_message
    if not reply_to:
        return False
    return bool(reply_to.from_user and reply_to.from_user.id == bot.id) or bool(
        getattr(reply_to, "via_bot", None) and reply_to.via_bot and reply_to.via_bot.id == bot.id
    )


def _bot_username(bot: Bot) -> str:
    """Username бота (без @). bot.me заполняется get_me() при старте."""
    try:
        return getattr(bot.me, "username", "") or ""
    except Exception:
        return ""


def _is_bot_mentioned(message: Message, bot: Bot) -> bool:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for e in entities:
        if e.type == MessageEntityType.TEXT_MENTION and e.user and e.user.id == bot.id:
            return True
        if e.type == MessageEntityType.MENTION:
            mention = text[e.offset:e.offset + e.length]
            if mention.lstrip("@") == _bot_username(bot):
                return True
    return False


def _strip_mentions(text: str, entities) -> str:
    """Убирает из текста @упоминания бота, возвращает чистый вопрос."""
    if not entities:
        return text
    chars = list(text)
    for e in sorted(entities, key=lambda x: x.offset, reverse=True):
        if e.type in (MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION):
            for i in range(e.offset, e.offset + e.length):
                chars[i] = " "
    return " ".join("".join(chars).split())


def _mentioned_username(message: Message, bot: Bot) -> str | None:
    """Возвращает @username другого участника, упомянутого в тексте (не бота)."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for e in entities:
        if e.type == MessageEntityType.MENTION:
            mention = text[e.offset:e.offset + e.length]
            uname = mention.lstrip("@")
            if uname.lower() != (_bot_username(bot) or "").lower() and uname:
                return uname
        if e.type == MessageEntityType.TEXT_MENTION:
            if e.user and e.user.id != bot.id:
                return e.user.username or str(e.user.id)
    return None


def _reply_target_user(message: Message, bot: Bot) -> Message | None:
    """Возвращает сообщение, на которое отвечают реплаем (если это НЕ бот)."""
    reply_to = message.reply_to_message
    if not reply_to or not reply_to.from_user:
        return None
    if reply_to.from_user.id == bot.id:
        return None
    if getattr(reply_to, "via_bot", None) and reply_to.via_bot and reply_to.via_bot.id == bot.id:
        return None
    return reply_to


def _is_about_person(question: str) -> bool:
    """Похоже ли, что вопрос — 'кто такой / расскажи о' ком-то."""
    q = question.lower()
    about_words = [
        "кто это", "кто такой", "кто такая", "кто таков", "о ком", "про кого",
        "расскажи о", "расскажи про", "опиши", "что за", "что ты знаешь о",
        "какой он человек", "расскажи об этом пользователе", "кто этот чел",
        "что он за человек", "что за человек",
        "кто он", "кто она", "расскажи кто", "чем занимается",
        "кем является", "что известно", "что о нем", "что о нём",
        "кто этот", "кто эта",
    ]
    return any(w in q for w in about_words)


def _brevity_request(text: str) -> str:
    """Если в вопросе просят коротко/одним словом — вернуть инструкцию длины.

    Возвращает "" если кратность не запрошена, иначе строку вроде "одним словом".
    """
    q = str(text or "").lower()
    brev = ""
    if "одним словом" in q or "одно слово" in q or "1 словом" in q or "одним словам" in q:
        brev = "одним словом"
    elif "нескольких словах" in q or "2-3 " in q or "пара слов" in q:
        brev = "несколькими словами"
    elif "кратко" in q or "коротко" in q or "покороче" in q or "вкратце" in q:
        brev = "кратко"
    return brev


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def handle_group_message(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    # Наблюдаем ВСЕ текстовые сообщения участников группы (не только
    # упоминания/реплаи) — копим переписку, чтобы строить заметки о личностях.
    await group_sessions.observe(
        message.chat.id,
        user_id,
        message.from_user.username,
        message.from_user.full_name,
        message.text or "",
    )
    _maybe_refresh_member_profile(message.chat.id, user_id, message)

    if _is_reply_to_bot(message, bot):
        await _handle_group_reply_continuation(message, bot, user_id)
        return

    if _is_bot_mentioned(message, bot):
        await _handle_group_mention(message, bot, user_id)


async def _download_image_data(bot: Bot, photo) -> str:
    """Скачать фото и вернуть data URL для vision-модели."""
    import base64
    file = await bot.get_file(photo.file_id)
    data = (await bot.download_file(file.file_path)).read()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


@router.message(F.chat.type.in_({"group", "supergroup"}), F.photo)
async def handle_group_photo(message: Message, bot: Bot) -> None:
    """Фото в группе (реплай на сообщение бота или @упоминание с фото-подписью) →
    анализ картинки vision-моделью, с контекстом группы."""
    user_id = message.from_user.id
    if user_storage.is_banned(user_id):
        return

    is_reply_bot = _is_reply_to_bot(message, bot)
    is_mention = _is_bot_mentioned(message, bot)
    logger.info("GROUP_PHOTO chat=%s uid=%s reply_to_bot=%s mentioned=%s cap=%r",
                message.chat.id, user_id, is_reply_bot, is_mention,
                (message.caption or "")[:60])

    if not is_reply_bot and not is_mention:
        return

    if not user_settings.is_multimodal_enabled():
        await message.reply("👁 Мультимодальность сейчас выключена (включите в админ-панели).")
        return

    if not await rate_limiter.allow(user_id):
        await message.reply("⏳ Слишком много сообщений подряд. Подождите немного.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        photo = message.photo[-1]
        data_url = await _download_image_data(bot, photo)
    except Exception as e:
        logger.error("Не удалось скачать фото в группе: %s", e)
        await message.reply("⚠️ Не удалось обработать фото.")
        return

    caption = (message.caption or "").strip()
    if _is_bot_mentioned(message, bot):
        caption = _strip_mentions(caption, message.caption_entities or [])
    question = caption or "Что на этом фото?"

    # Знания о участниках чата + live-профили.
    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = _build_group_system_prompt(
        message.chat.id, user_id, profile, user_custom_prompt,
    )
    history = group_sessions.get_history(message.chat.id)

    model_id = MODEL_MAP["vision"]
    use_thinking = False

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, question, history=history,
            model=model_id, use_thinking=use_thinking, images=[data_url],
        )
    except Exception as e:
        logger.error("Ошибка при ответе на фото в группе: %s", e)
        answer = "⚠️ Произошла ошибка при анализе фото. Попробуйте позже."

    await _reply_with_quote(message, bot, question, answer)

    await group_sessions.append_message(message.chat.id, "user", f"[фото] {question}")
    await group_sessions.append_message(message.chat.id, "assistant", answer)


def _maybe_refresh_member_profile(chat_id: int, user_id: int, message: Message) -> None:
    """Если профиль участника ещё не строился или накопились новые сообщения —
    запускаем фоновое обновление по его последним сообщениям в чате.
    """
    current = group_sessions.get_member_profile(chat_id, user_id)
    recent = group_sessions.get_member_log(chat_id, user_id, limit=15)
    # Профиль уже есть, а новых сообщений мало — не тратим запрос DeepSeek зря.
    if current and len(recent) < 5:
        return
    asyncio.get_event_loop().create_task(
        _refresh_member_profile_task(chat_id, user_id, message, current, recent)
    )


async def _refresh_member_profile_task(
    chat_id: int, user_id: int, message: Message, old: str, recent: list
) -> None:
    """Фоново обновляет заметку об участнике группы в отдельной задаче."""
    try:
        member_name = message.from_user.full_name or message.from_user.username or str(user_id)
        texts = [m.get("text", "") for m in recent if m.get("text")]
        new_profile = await summarize_member_profile(
            chat_id, user_id, member_name, old, texts
        )
        if new_profile != old:
            await group_sessions.set_member_profile(chat_id, user_id, new_profile)
            logger.info("Обновлён профиль участника %s в группе %s", user_id, chat_id)
    except Exception as e:
        logger.error("Ошибка фонового обновления профиля участника: %s", e)


def _build_group_system_prompt(chat_id: int, user_id: int, profile: str, user_custom_prompt: str) -> str:
    """Системный промт для ответов в группе: добавляем знания о участниках чата
    (имена, портреты, кликухи из экспорта + live-профили), чтобы бот учитывал
    личности и обстановку в группе, а не только историю диалога.
    """
    base = build_full_system_prompt(profile, user_custom_prompt)

    parts = []

    # Знания из экспорта чата: ростер имён + портреты + локальные кликухи.
    try:
        knowledge_note = member_knowledge.build_group_context(chat_id, user_id)
        if knowledge_note:
            parts.append(knowledge_note)
    except Exception as e:
        logger.error("Не удалось собрать знания об участниках для группы %s: %s", chat_id, e)

    # Live-профили участников (наблюдение при работающем боте).
    profiles = group_sessions.get_member_profiles(chat_id)
    if profiles:
        lines = [f"• ID {mid}: {note}" for mid, note in profiles.items() if note]
        if lines:
            parts.append(
                "Вот что бот знает об участниках ЭТОГО группового чата из наблюдения "
                "за перепиской (используй как контекст, не зачитывай дословно и не "
                "упоминай, что эти заметки существуют):\n" + "\n".join(lines)
            )

    if parts:
        return base + "\n\n" + "\n\n".join(parts)
    return base


async def _handle_group_reply_continuation(message: Message, bot: Bot, user_id: int) -> None:
    """Продолжение диалога: кто-то ответил реплаем на сообщение бота в группе.

    Контекст берём из той же изолированной истории группы (group_sessions),
    что и при @упоминании — чтобы продолжение реплаем сохраняло контекст.
    """
    if user_storage.is_banned(user_id):
        return

    if not await rate_limiter.allow(user_id):
        await message.reply("⏳ Слишком много сообщений подряд. Подождите немного.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    question = message.text or message.caption or ""
    history = group_sessions.get_history(message.chat.id)

    chat_context = await _maybe_chat_context(question, message.chat.id)

    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = _build_group_system_prompt(
        message.chat.id, user_id, profile, user_custom_prompt,
    )

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, question, history=history,
            model=model_id, use_thinking=use_thinking,
            chat_context=chat_context,
        )
    except Exception as e:
        logger.error("Ошибка при ответе в группе: %s", e)
        answer = "⚠️ Произошла ошибка. Попробуйте позже."

    await _reply_with_quote(message, bot, question, answer)

    await group_sessions.append_message(message.chat.id, "user", question)
    await group_sessions.append_message(message.chat.id, "assistant", answer)


async def _handle_personality_request(message: Message, bot: Bot, user_id: int, question: str) -> bool:
    """Если пользователь спрашивает о личности другого участника (реплай на него
    или @username в тексте + просьба 'кто это/расскажи о') — строим описание
    по его сообщениям в этой группе и отвечаем. Иначе возвращаем False.
    """
    chat_id = message.chat.id

    # Кандидат №1: реплай на сообщение другого участника (не бота).
    target_id = None
    target_name = None
    reply_target = _reply_target_user(message, bot)
    if reply_target:
        target_id = reply_target.from_user.id
        target_name = reply_target.from_user.full_name or reply_target.from_user.username or str(target_id)

    # Кандидат №2: @username в тексте вопроса.
    uname = _mentioned_username(message, bot)
    if uname:
        member_id = group_sessions.find_member_by_username(chat_id, uname)
        if member_id is not None:
            target_id = member_id
            target_name = group_sessions.member_display_name(chat_id, member_id) or uname

    if target_id is None:
        logger.info("PERSONA: цель не найдена chat=%s uname=%r", chat_id, uname)
        return False
    if not _is_about_person(question):
        logger.info("PERSONA: вопрос не о личности chat=%s target=%s q=%r", chat_id, target_id, question)
        return False

    logger.info("PERSONA: вопрос о личности chat=%s target=%s name=%r q=%r", chat_id, target_id, target_name, question)

    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    if target_id == user_id:
        await _reply_with_quote(message, bot, question, "Ты спрашиваешь про себя — а самого себя видно со стороны лучше :)")
        return True

    # Берём до 250 сообщений из ПОЛНОЙ истории (экспорт юзербота + live).
    texts = group_sessions.get_full_member_log(chat_id, target_id, limit=250)
    logger.info("PERSONA: найдено сообщений об участнике %s: %d", target_id, len(texts))

    # Если историю этого участника ещё не экспортировали (мало сообщений) —
    # пробуем сами через юзербота добрать его сообщения из чата (до вступления бота).
    if len(texts) < 25:
        status = None
        try:
            status = await message.reply("Собираю историю сообщений, это займёт ~20-30 сек...")
        except Exception:
            pass
        exported = await _run_userbot_export_now(chat_id, target_id, limit=2000)
        if exported:
            texts = group_sessions.get_full_member_log(chat_id, target_id, limit=250)
        elif status is not None:
            try:
                await status.edit_text("Продолжаю по уже известным сообщениям.")
            except Exception:
                pass

    if not texts:
        await _reply_with_quote(
            message, bot, question,
            "Пока слишком мало сообщений об этом участнике, чтобы я мог его описать. "
            "Пусть он пообщается в чате — и я соберу портрет.",
        )
        return True

    member_name = f"@{target_name}" if target_name else str(target_id)

    # Если просят коротко/одним словом — не берём из кэша, а строим нужного объёма.
    brev = _brevity_request(question)

    # Кэш по @username: повторно не тратим токены на описание личности.
    if uname and not brev:
        cached = group_sessions.get_cached_personality(uname)
        if cached is not None:
            logger.info("PERSONA: портрет %s из кэша (сообщений: %s)", member_name, cached.get("message_count"))
            description = cached["personality"]
        else:
            description = await describe_personality(member_name, texts[-200:], brevity=brev)
            group_sessions.set_cached_personality(uname, description, len(texts))
    else:
        description = await describe_personality(member_name, texts[-200:], brevity=brev)

    await _reply_with_quote(message, bot, question, description)

    await group_sessions.append_message(chat_id, "user", f"Про {member_name}: {question}")
    await group_sessions.append_message(chat_id, "assistant", description)
    return True


async def _handle_group_mention(message: Message, bot: Bot, user_id: int) -> None:
    """Пользователь упомянул @bot в группе — бот отвечает реплаем на это сообщение."""
    if user_storage.is_banned(user_id):
        return

    if not await rate_limiter.allow(user_id):
        await message.reply("⏳ Слишком много сообщений подряд. Подождите немного.")
        return

    question = _strip_mentions(message.text or "", message.entities or message.caption_entities)
    if not question:
        await message.reply("Задай вопрос после упоминания бота.")
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    personality = await _handle_personality_request(message, bot, user_id, question)
    if personality is not None:
        return

    history = group_sessions.get_history(message.chat.id)

    chat_context = await _maybe_chat_context(question, message.chat.id)

    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = _build_group_system_prompt(
        message.chat.id, user_id, profile, user_custom_prompt,
    )

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        answer = await ask_deepseek_with_search(
            system_prompt, question, history=history,
            model=model_id, use_thinking=use_thinking,
            chat_context=chat_context,
        )
    except Exception as e:
        logger.error("Ошибка при ответе на упоминание в группе: %s", e)
        answer = "⚠️ Произошла ошибка. Попробуйте позже."

    await _reply_with_quote(message, bot, question, answer)

    await group_sessions.append_message(message.chat.id, "user", question)
    await group_sessions.append_message(message.chat.id, "assistant", answer)


# ============================================================
#  Инлайн-режим — Guest Mode (@bot запрос)
# ============================================================
#  Инлайн-режим — Guest Mode (@bot запрос)
# ============================================================

INLINE_DEEPSEEK_TIMEOUT = 90


async def _maybe_chat_context(query_text: str, chat_id=None) -> str:
    """Если вопрос явно про людей/содержимое чата — вернуть релевантный отрывок
    переписки из экспорта (иначе пустую строку).

    Сначала проверяем по известным именам/кликухам участников (детерминированно,
    без лишнего AI-запроса), затем при необходимости — AI-решением.
    """
    try:
        # Детерминированная проверка: есть ли в запросе имя/кликуха из базы.
        if member_knowledge.query_refers_to_chat_member(query_text):
            ctx = member_knowledge.search_exported_messages(
                query_text, chat_id=chat_id, limit=15
            )
            if ctx:
                logger.info("CHAT_SEARCH: имя/кликуха по запросу %r (chars=%d)", query_text[:80], len(ctx))
            return ctx

        if not await member_knowledge.ai_decides_chat_search(query_text):
            return ""
        ctx = member_knowledge.search_exported_messages(
            query_text, chat_id=chat_id, limit=15
        )
        if ctx:
            logger.info("CHAT_SEARCH: найден контекст по запросу %r (chars=%d)", query_text[:80], len(ctx))
        return ctx
    except Exception as e:
        logger.error("CHAT_SEARCH ошибка: %s", e)
        return ""


async def _get_ai_answer(user_id: int, query_text: str, bot_username: str = "") -> str:
    """Получить ответ ИИ для inline/guest запроса."""
    # Запрос "кто такой @X" — берём из базы (без интернет-поиска), если есть данные.
    persona = await _try_global_personality(query_text, bot_username)
    if persona is not None:
        return persona

    # Если вопрос касается людей/содержимого чата — ищем ответ в экспорте, а не в интернете.
    chat_context = await _maybe_chat_context(query_text)

    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = build_full_system_prompt(profile, user_custom_prompt)

    model_id = user_settings.get_model_id(user_id)
    use_thinking = user_settings.use_thinking(user_id)

    try:
        return await asyncio.wait_for(
            ask_deepseek_with_search(
                system_prompt, query_text, model=model_id, use_thinking=use_thinking,
                chat_context=chat_context,
            ),
            timeout=INLINE_DEEPSEEK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return "⚠️ Превышен таймаут ответа. Попробуйте более короткий вопрос."
    except Exception as e:
        logger.error("INLINE ошибка DeepSeek: %s", e)
        return "⚠️ Ошибка при обращении к нейросети."


def _quote_query(query_text: str) -> str:
    """Цитата вопроса пользователя в blockquote (HTML)."""
    escaped = html.escape(query_text, quote=False).replace("\n", "<br/>")
    return f"<blockquote>{escaped}</blockquote>"


def _format_inline_answer(query_text: str, answer: str) -> str:
    """Финальный HTML ответа для инлайна: вопрос в кавычках + ответ ИИ."""
    html_answer = markdown_to_html(answer)
    if not query_text:
        return wrap_long_answer(html_answer)
    html_answer = f"{_quote_query(query_text)}\n\n{html_answer}"
    return wrap_long_answer(html_answer)


async def _reply_with_quote(message: Message, bot: Bot, question: str, answer: str) -> None:
    """Отправляет реплай в чат: вопрос в кавычках + ответ ИИ."""
    html_answer = markdown_to_html(answer)
    if question:
        html_answer = f"{_quote_query(question)}\n\n{html_answer}"
    html_answer = wrap_long_answer(html_answer)
    try:
        await message.reply(html_answer)
    except TelegramBadRequest as e:
        logger.error("Не удалось отправить как HTML (%s), шлю обычным текстом", e)
        plain = f"«{question}»\n\n{answer}" if question else answer
        try:
            await message.reply(plain, parse_mode=None)
        except TelegramBadRequest:
            await message.reply(answer, parse_mode=None)


async def _run_userbot_export_now(chat_id: int, user_id: int, limit: int) -> bool:
    """Запускает userbot_export.py как подпроцесс для конкретного участника.

    Возвращает True, если экспорт прошёл успешно. Используется для автосбора
    истории участника по запросу его личности.
    """
    import os
    import sys

    if not (config.USERBOT_API_ID and config.USERBOT_API_HASH):
        return False

    cmd = [
        sys.executable, "userbot_export.py",
        "--chat", str(chat_id),
        "--user", str(user_id),
        "--limit", str(limit),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(os.path.abspath(__file__)) + "/..",
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("Автоэкспорт участника %s превысил таймаут", user_id)
            return False
        return proc.returncode == 0
    except Exception as e:
        logger.error("Автоэкспорт участника %s не удался: %s", user_id, e)
        return False


# --- Inline query: показать бота в панели инлайна ---

@router.inline_query()
async def handle_inline(inline_query: InlineQuery) -> None:
    """Показываем результат в панели инлайна.

    Обработка выбора идёт через guest_message (Guest Mode) или
    chosen_inline_result (fallback).
    """
    query_text = inline_query.query.strip()

    if not query_text:
        await inline_query.answer([], cache_time=1, is_personal=True)
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


# --- Guest Mode: основной путь для @bot запросов ---

async def _answer_guest_query_with_result(message: Message, bot: Bot, query_text: str, answer: str) -> None:
    """Ответить через answerGuestQuery — правильный метод для Guest Mode."""
    html_answer = _format_inline_answer(query_text, answer)
    result = InlineQueryResultArticle(
        id="answer",
        title="Ответ ИИ",
        description=answer[:100],
        input_message_content=InputTextMessageContent(
            message_text=html_answer,
            parse_mode=ParseMode.HTML,
        ),
    )
    try:
        await bot.answer_guest_query(
            guest_query_id=message.guest_query_id,
            result=result,
        )
        return
    except TelegramBadRequest as e:
        logger.warning("GUEST answerGuestQuery HTML failed: %s", e)

    result_plain = InlineQueryResultArticle(
        id="answer",
        title="Ответ ИИ",
        description=answer[:100],
        input_message_content=InputTextMessageContent(
            message_text=f"«{query_text}»\n\n{answer}" if query_text else answer,
        ),
    )
    try:
        await bot.answer_guest_query(
            guest_query_id=message.guest_query_id,
            result=result_plain,
        )
    except Exception as e2:
        logger.error("GUEST answerGuestQuery plain failed: %s", e2)


@router.guest_message()
async def handle_guest_message(message: Message, bot: Bot) -> None:
    """Guest Mode: @bot запрос в любом чате.

    Telegram присылает guest_message с guest_query_id.
    ОБЯЗАТЕЛЬНО отвечаем через answerGuestQuery — иначе крестик.

    ВАЖНО про ЛС: из-за Guest Mode обычный текст в ЛС может прийти НЕ как
    message, а как guest_message. Тогда это ЛС (chat.type == "private") —
    отвечаем ОБЫЧНЫМ сообщением (message.answer), чтобы пользователь видел
    ответ как нормальное сообщение в ЛС, а не только inline-превью.
    """
    chat_type = getattr(message.chat, "type", None)
    is_private = chat_type == "private"
    query_text = (message.text or "").strip()

    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return

    # Фото в guest-сообщении (прикреплённое к inline-запросу @bot/prёжде) →
    # анализируем картинку vision-моделью вместо шаблона-подсказки.
    if not query_text or message.photo or message.animation:
        photo = message.photo[-1] if message.photo else None
        if photo is not None and user_settings.is_multimodal_enabled() and not user_storage.is_banned(user_id):
            await _handle_guest_photo(message, bot, user_id, photo,
                                      caption=(message.caption or "").strip())
            return

    if not query_text:
        if is_private:
            await message.answer(
                "Напишите сообщение — и я отвечу, помня контекст разговора. "
                "Команда <code>/start</code> — помощь."
            )
        else:
            # В группе на пустой guest (гиф/фото без вопроса) молчим,
            # чтобы не спамить шаблоном-подсказкой.
            logger.info("GUEST пустой запрос в группе %s от %s — игнор", message.chat.id, user_id)
        return

    if user_storage.is_banned(user_id):
        return

    if not await rate_limiter.allow(user_id):
        if is_private:
            await message.answer("⏳ Слишком много сообщений подряд. Подождите немного.")
        else:
            await _answer_guest_query_with_result(
                message, bot, query_text,
                "⏳ Слишком много запросов. Подождите.",
            )
        return

    await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)

    answer = await _get_ai_answer(user_id, query_text, _bot_username(bot))

    if is_private:
        # ЛС-гость: шлём обычное сообщение (как в handle_text), сохраняем контекст чата.
        try:
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception:
            pass
        await _safe_answer(message, answer)
        await chat_sessions.append_message(user_id, "user", query_text)
        await chat_sessions.append_message(user_id, "assistant", answer)
    else:
        await _answer_guest_query_with_result(message, bot, query_text, answer)
        history = [{"role": "user", "content": query_text}, {"role": "assistant", "content": answer}]
        await reply_context_store.save_context(answer, history, user_id)


async def _handle_guest_photo(message: Message, bot: Bot, user_id: int, photo, caption: str) -> None:
    """Анализ фото, присланного в guest-сообщении (@bot + фото в инлайн-запросе)."""
    if not await rate_limiter.allow(user_id):
        return

    try:
        await user_storage.touch(user_id, message.from_user.username, message.from_user.full_name)
    except Exception:
        pass

    try:
        data_url = await _download_image_data(bot, photo)
    except Exception as e:
        logger.error("GUEST фото: не удалось скачать (%s)", e)
        await _answer_guest_query_with_result(message, bot, caption or "", "⚠️ Не удалось обработать фото.")
        return

    question = caption or "Что на этом фото?"
    profile = user_storage.get_profile(user_id)
    user_custom_prompt = user_settings.get_system_prompt(user_id)
    system_prompt = build_full_system_prompt(profile, user_custom_prompt)

    model_id = MODEL_MAP["vision"]
    try:
        answer = await asyncio.wait_for(
            ask_deepseek_with_search(
                system_prompt, question, model=model_id, use_thinking=False, images=[data_url],
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        answer = "⚠️ Превышен таймаут анализа фото."
    except Exception as e:
        logger.error("GUEST фото: ошибка DeepSeek (%s)", e)
        answer = "⚠️ Произошла ошибка при анализе фото. Попробуйте позже."

    await _answer_guest_query_with_result(message, bot, question, answer)


# --- Fallback: chosen_inline_result (когда Guest Mode не активен) ---

async def _try_global_personality(query_text: str, bot_username: str) -> str | None:
    """Попытка ответить на 'кто такой @X' из базы (экспорт юзербота) в инлайн-пути.

    Ищем сообщения участника по @username ИЛИ по user_id (user123 или 123) во ВСЕХ
    чатах, собранных юзерботом. Если данных достаточно — возвращаем портрет из базы
    (БЕЗ интернет-поиска, markdown). Если данных нет — вернём None и пойдёт обычный путь.
    """
    if not _is_about_person(query_text):
        return None

    # Определяем идентификатор: @username или user_id (user8065762277 / 8065762277).
    ident = _extract_identifier(query_text)
    if not ident:
        return None
    key, is_id = ident
    if not is_id and key.lower() == (bot_username or "").lower():
        return None

    # Если просят коротко/одним словом — не берём из кэша полный портрет,
    # а перегенерируем нужного объёма.
    brev = _brevity_request(query_text)

    # Кэш: если портрет уже составлен — отдаём его, не тратя токены.
    if not brev:
        cached = group_sessions.get_cached_personality(key)
        if cached is not None:
            logger.info("GLOBAL_PERSONA: портрет %s из кэша (сообщений: %s)", key, cached.get("message_count"))
            return cached["personality"]

    if is_id:
        texts = database.get_member_log_all_for_user_global(key, limit=300)
        display = group_sessions.member_display_name_global(key)
    else:
        texts = group_sessions.get_global_member_log(key, limit=300)
        display = f"@{key}"

    if len(texts) < 15:
        logger.info("GLOBAL_PERSONA: нет данных по %s (%d сообщений)", key, len(texts))
        return (
            f"По участнику <code>{display or key}</code> пока недостаточно сведений, "
            "чтобы составить портрет — в базе меньше 15 его сообщений. Пусть он "
            "напишет в чат, и я соберу его описание.\n\n"
            f"Нашёл сообщений: {len(texts)}."
        )
    logger.info("GLOBAL_PERSONA: %s сообщений для %s (из базы, без интернет-поиска)", len(texts), key)
    portrait = await describe_personality(display or key, texts[-200:], brevity=brev)
    if not brev:
        group_sessions.set_cached_personality(key, portrait, len(texts))
    return portrait


def _extract_identifier(text: str):
    """Извлечь из запроса идентификатор участника.

    Возвращает (ключ, is_id) где:
      - @username  → ("username", False)
      - user8065.. → ("8065..", True)
      - 8065..     → ("8065..", True)
    Или None, если идентификатора нет.
    """
    t = text or ""
    m = re.search(r"user(\d{5,})", t, re.IGNORECASE)
    if m:
        return m.group(1), True
    m = re.search(r"@([A-Za-z0-9_]{4,})", t)
    if m:
        return m.group(1), False
    m = re.search(r"(?<![@\d])(\d{7,})", t)
    if m:
        return m.group(1), True
    return None


def _extract_username(text: str) -> str | None:
    m = re.search(r"@([A-Za-z0-9_]{4,})", text or "")
    return m.group(1) if m else None


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot) -> None:
    """Fallback: редактируем сообщение через inline_message_id."""
    inline_message_id = chosen.inline_message_id
    user_id = chosen.from_user.id
    query_text = chosen.query.strip()

    if not inline_message_id or not query_text:
        return

    if user_storage.is_banned(user_id):
        return

    if not await rate_limiter.allow(user_id):
        try:
            await bot.edit_message_text(
                "⏳ Слишком много запросов. Подождите.",
                inline_message_id=inline_message_id,
            )
        except Exception:
            pass
        return

    await user_storage.touch(user_id, chosen.from_user.username, chosen.from_user.full_name)

    # Однократный статус "думает" (частый edit в цикле вызывает flood control)
    try:
        await bot.edit_message_text("⏳ Думаю...", inline_message_id=inline_message_id)
    except Exception:
        pass

    answer = await _get_ai_answer(user_id, query_text, _bot_username(bot))

    # --- Облёгчённый typewriter + обязательный финальный edit с retry при flood ---
    # Telegram жёстко лимитирует editMessageText (~1/сек на чат). Частые edit
    # (как анимация каждую секунду + typewriter каждые 0.35с) провоцируют
    # "Flood control exceeded" и ответ теряется. Поэтому печатаем редко и у
    # финального edit есть retry с паузой.
    await _typewriter_edit(bot, inline_message_id, answer)

    final_html = _format_inline_answer(query_text, answer)
    await _edit_with_retry(
        bot,
        inline_message_id=inline_message_id,
        html=final_html,
        plain=f"«{query_text}»\n\n{answer}" if query_text else answer,
    )

    history = [{"role": "user", "content": query_text}, {"role": "assistant", "content": answer}]
    await reply_context_store.save_context(answer, history, user_id)


async def _typewriter_edit(bot: Bot, inline_message_id: str, answer: str) -> None:
    """Редкая typewriter-анимация. Telegram лимитирует editMessageText
    (~1/сек на чат), поэтому используем НЕ больше 4-5 кадров с паузами >=1.5с.
    Любая ошибка (в т.ч. flood) — пропускаем кадр и идём дальше.
    """
    steps = min(4, max(2, len(answer) // 120))
    chunk_size = max(1, len(answer) // steps)
    cuts = list(range(chunk_size, len(answer), chunk_size))
    for i, cut in enumerate(cuts, 1):
        try:
            await bot.edit_message_text(
                answer[:cut] + " ▌",
                inline_message_id=inline_message_id,
                parse_mode=None,
            )
        except Exception:
            pass  # flood / message not modified — не критично, пропускаем
        await asyncio.sleep(1.5 if i < len(cuts) else 0)


async def _edit_with_retry(bot: Bot, inline_message_id: str, html: str, plain: str) -> None:
    """Финальный edit с повторными попытками при flood control

    (Telegram: 'Too Many Requests: retry after N' — на editMessageText лимит
    достигается легко, и без retry ответ просто терялся).
    """
    delay = 2.0
    for attempt in range(4):
        try:
            await bot.edit_message_text(html, inline_message_id=inline_message_id)
            return
        except TelegramBadRequest as e:
            # пробуем обычным текстом (без HTML) один раз
            try:
                await bot.edit_message_text(
                    plain, inline_message_id=inline_message_id, parse_mode=None,
                )
                return
            except Exception:
                pass
            # flood control — ждём и пробуем снова
            retry_after = _extract_retry_after(e)
            await asyncio.sleep(retry_after or delay)
            delay *= 2
        except Exception as e:
            logger.error("INLINE edit failed (attempt %d/4): %s", attempt + 1, e)
            await asyncio.sleep(delay)
            delay *= 2
    logger.error("INLINE: не удалось отправить финальный ответ после 4 попыток")


def _extract_retry_after(exc: Exception) -> int | None:
    """Достаёт число секунд из 'retry after N' в тексте ошибки."""
    text = str(exc)
    match = re.search(r"retry\s+after\s+(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None
