import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from filters import IsAdmin
from keyboards import admin_menu_kb, cancel_kb, confirm_broadcast_kb
from md_format import markdown_to_html
from model_manager import model_manager
from prompt_manager import prompt_manager
from states import AdminStates
from user_settings import user_settings
from user_storage import user_storage

logger = logging.getLogger(__name__)

router = Router(name="admin")
# Все хэндлеры этого роутера доступны только администратору
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()  # на случай, если админ был в процессе какого-то из сценариев
    await message.answer(
        "🔧 <b>Админ-панель</b>\n"
        f"Модель DeepSeek: <code>{model_manager.get()}</code>\n"
        f"Пользователей в базе: {user_storage.count()}\n\n"
        "Выберите действие:",
        reply_markup=admin_menu_kb(),
    )


# ============================================================
#  Системный промт
# ============================================================

@router.callback_query(F.data == "admin_show_prompt")
async def show_prompt(callback: CallbackQuery) -> None:
    current = prompt_manager.get()
    await callback.message.answer(
        f"📄 Текущий системный промт (редактируемая часть):\n\n<code>{current}</code>\n\n"
        "Помимо этого, к каждому запросу всегда добавляется неизменяемая "
        "инструкция, защищающая личность бота и сам промт от раскрытия — "
        "она не редактируется через панель."
    )
    await callback.answer()


@router.callback_query(F.data == "admin_edit_prompt")
async def edit_prompt_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_new_prompt)
    await callback.message.answer(
        "✏️ Отправьте новый текст системного промта одним сообщением.",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Отменено.", reply_markup=admin_menu_kb())
    await callback.answer()


@router.message(AdminStates.waiting_for_new_prompt, F.text)
async def edit_prompt_finish(message: Message, state: FSMContext) -> None:
    new_prompt = message.text.strip()

    if not new_prompt:
        await message.answer(
            "⚠️ Промт не может быть пустым. Отправьте текст ещё раз или нажмите «Отмена».",
            reply_markup=cancel_kb(),
        )
        return

    await prompt_manager.set(new_prompt)
    await state.clear()

    await message.answer("✅ Системный промт успешно обновлён!", reply_markup=admin_menu_kb())
    logger.info("Администратор %s обновил системный промт", message.from_user.id)


@router.message(AdminStates.waiting_for_new_prompt)
async def edit_prompt_wrong_type(message: Message) -> None:
    await message.answer(
        "⚠️ Пришлите новый промт текстовым сообщением, либо нажмите «Отмена».",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data == "admin_reset_prompt")
async def reset_prompt(callback: CallbackQuery) -> None:
    await prompt_manager.reset()
    await callback.message.answer(
        "🔄 Системный промт сброшен к значению по умолчанию.",
        reply_markup=admin_menu_kb(),
    )
    await callback.answer()
    logger.info("Администратор %s сбросил системный промт к дефолтному", callback.from_user.id)


@router.callback_query(F.data == "admin_toggle_thinking")
async def toggle_thinking(callback: CallbackQuery) -> None:
    new_state = not user_settings.is_thinking_enabled()
    await user_settings.set_thinking_enabled(new_state)
    status = "ВКЛЮЧЕНА — пользователи могут выбирать 🧠 думающую модель" if new_state else "ВЫКЛЮЧЕНА — все отвечают быстро"
    await callback.answer("✅ Изменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_menu_kb())
    except TelegramBadRequest:
        pass
    await callback.message.answer(f"🧠 Думающая модель: {status}.")
    logger.info("Админ %s изменил глобальную думающую модель: %s", callback.from_user.id, new_state)


@router.callback_query(F.data == "admin_toggle_multimodal")
async def toggle_multimodal(callback: CallbackQuery) -> None:
    new_state = not user_settings.is_multimodal_enabled()
    await user_settings.set_multimodal_enabled(new_state)
    status = "ВКЛЮЧЕНА — бот принимает фото с текстом" if new_state else "ВЫКЛЮЧЕНА — бот не принимает фото"
    await callback.answer("✅ Изменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_menu_kb())
    except TelegramBadRequest:
        pass
    await callback.message.answer(f"👁 Мультимодальность: {status}.")
    logger.info("Админ %s изменил глобальную мультимодальность: %s", callback.from_user.id, new_state)


# ============================================================
#  Модель DeepSeek
# ============================================================

@router.callback_query(F.data == "admin_change_model")
async def change_model_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_model_name)
    await callback.message.answer(
        f"Текущая модель: <code>{model_manager.get()}</code>\n\n"
        "Отправьте название новой модели DeepSeek (например, "
        "<code>deepseek-chat</code> или <code>deepseek-reasoner</code>).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_model_name, F.text)
async def change_model_finish(message: Message, state: FSMContext) -> None:
    new_model = message.text.strip()
    if not new_model:
        await message.answer("⚠️ Название модели не может быть пустым.", reply_markup=cancel_kb())
        return
    await model_manager.set(new_model)
    await state.clear()
    await message.answer(f"✅ Модель изменена на <code>{new_model}</code>.", reply_markup=admin_menu_kb())
    logger.info("Администратор %s сменил модель DeepSeek на %s", message.from_user.id, new_model)


# ============================================================
#  Статистика
# ============================================================

@router.callback_query(F.data == "admin_stats")
async def show_stats(callback: CallbackQuery) -> None:
    count = user_storage.count()
    banned = sum(1 for uid in user_storage.get_all() if user_storage.is_banned(uid))
    await callback.message.answer(
        f"📊 Пользователей в базе: <b>{count}</b>\n"
        f"Из них забанено: <b>{banned}</b>\n"
        f"Текущая модель DeepSeek: <code>{model_manager.get()}</code>"
    )
    await callback.answer()


# ============================================================
#  Пользователи в базе
# ============================================================

@router.callback_query(F.data == "admin_view_users")
async def view_users_list(callback: CallbackQuery, state: FSMContext) -> None:
    all_ids = user_storage.get_all()
    if not all_ids:
        await callback.message.answer("База пользователей пуста.", reply_markup=admin_menu_kb())
        await callback.answer()
        return

    lines = ["👥 <b>Пользователи в базе:</b>\n"]
    for i, uid in enumerate(sorted(all_ids), 1):
        rec = user_storage.get_record(uid)
        if not rec:
            continue
        username = f"@{rec['username']}" if rec.get("username") else "—"
        name = rec.get("full_name") or "—"
        banned = " 🚫" if rec.get("banned") else ""
        lines.append(f"{i}. <code>{uid}</code> — {username} — {name}{banned}")

    lines.append("\nВведите Telegram ID для просмотра карточки:")
    await state.set_state(AdminStates.waiting_for_profile_user_id)
    await callback.message.answer("\n".join(lines), reply_markup=cancel_kb())
    await callback.answer()


@router.message(AdminStates.waiting_for_profile_user_id, F.text)
async def view_profile_finish(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await state.clear()

    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ ID должен быть числом.", reply_markup=admin_menu_kb())
        return

    user_id = int(text)
    record = user_storage.get_record(user_id)
    if record is None:
        await message.answer("Пользователь с таким ID не найден в базе.", reply_markup=admin_menu_kb())
        return

    profile_text = record.get("profile") or "— пока нет данных —"
    status = "🚫 забанен" if record.get("banned") else "активен"
    username = f"@{record['username']}" if record.get("username") else "—"

    model_key = user_settings.get_model(user_id)
    model_label = "⚡ Быстрая" if model_key == "fast" else "🧠 Думающая"
    custom_prompt = user_settings.get_system_prompt(user_id)
    custom_prompt_display = f"<code>{custom_prompt}</code>" if custom_prompt else "— не задан —"

    await message.answer(
        f"👤 <b>Пользователь {user_id}</b>\n"
        f"Имя: {record.get('full_name') or '—'}\n"
        f"Username: {username}\n"
        f"Статус: {status}\n"
        f"Сообщений отправлено: {record.get('message_count', 0)}\n"
        f"Первое обращение: {record.get('first_seen', '—')}\n"
        f"Последняя активность: {record.get('last_seen', '—')}\n\n"
        f"⚙️ Модель: {model_label}\n"
        f"🧠 Пользовательский промпт:\n{custom_prompt_display}\n\n"
        f"📝 Заметка ИИ о пользователе (интересы/факты):\n{profile_text}",
        reply_markup=admin_menu_kb(),
    )
    logger.info("Администратор %s посмотрел профиль пользователя %s", message.from_user.id, user_id)


# ============================================================
#  Просмотр ЛС-переписки пользователя с ботом
# ============================================================

@router.callback_query(F.data == "admin_view_user_messages")
async def view_user_messages_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_view_messages_user_id)
    await callback.message.answer(
        "Введите Telegram ID пользователя, чью переписку в ЛС с ботом хотите посмотреть.\n"
        "(ID можно узнать через @userinfobot)",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_view_messages_user_id, F.text)
async def view_user_messages_finish(message: Message, state: FSMContext) -> None:
    import chat_sessions

    text = message.text.strip()
    await state.clear()

    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ ID должен быть числом.", reply_markup=admin_menu_kb())
        return

    user_id = int(text)
    history = chat_sessions.get_history(user_id)
    if not history:
        await message.answer("У этого пользователя нет активной ЛС-переписки с ботом.", reply_markup=admin_menu_kb())
        return

    chat_name = chat_sessions.get_active_chat_name(user_id)
    lines = [f"💬 <b>ЛС-переписка с @{message.from_user.username or 'вы'}:</b>\n", f"Чат: {chat_name}\n"]
    for entry in history[-30:]:
        role = "👤" if entry.get("role") == "user" else "🤖"
        content = str(entry.get("content", ""))[:1500]
        lines.append(f"<b>{role}</b> {content}\n")

    await message.answer("\n".join(lines), reply_markup=admin_menu_kb())
    logger.info("Администратор %s посмотрел ЛС-переписку пользователя %s", message.from_user.id, user_id)



# ============================================================
#  Личное сообщение конкретному пользователю
# ============================================================

@router.callback_query(F.data == "admin_direct_message")
async def direct_message_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_direct_user_id)
    await callback.message.answer(
        "Введите Telegram ID пользователя, которому хотите написать.\n"
        "(ID можно узнать, например, через @userinfobot)",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_direct_user_id, F.text)
async def direct_message_get_id(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.lstrip("-").isdigit():
        await message.answer(
            "⚠️ ID должен быть числом. Отправьте корректный Telegram ID или нажмите «Отмена».",
            reply_markup=cancel_kb(),
        )
        return

    user_id = int(text)
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminStates.waiting_for_direct_message)

    warning = ""
    if not user_storage.contains(user_id):
        warning = (
            "\n\n⚠️ Этого ID нет в базе (пользователь ни разу не писал боту в личку) — "
            "доставка может не сработать, но попытка всё равно будет сделана."
        )

    await message.answer(
        f"Введите текст сообщения, которое нужно отправить пользователю <code>{user_id}</code>.{warning}",
        reply_markup=cancel_kb(),
    )


@router.message(AdminStates.waiting_for_direct_message, F.text)
async def direct_message_send(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user_id = data.get("target_user_id")
    text = markdown_to_html(message.text)
    await state.clear()

    try:
        await bot.send_message(user_id, text)
        await message.answer("✅ Сообщение доставлено.", reply_markup=admin_menu_kb())
        logger.info("Админ %s отправил личное сообщение пользователю %s", message.from_user.id, user_id)
    except TelegramForbiddenError:
        await user_storage.remove(user_id)
        await message.answer(
            "⚠️ Пользователь заблокировал бота (или никогда его не запускал) — сообщение не доставлено.",
            reply_markup=admin_menu_kb(),
        )
    except TelegramBadRequest as e:
        await message.answer(f"⚠️ Не удалось отправить: некорректный ID или чат не найден ({e}).", reply_markup=admin_menu_kb())
    except Exception as e:
        logger.error("Ошибка при отправке личного сообщения пользователю %s: %s", user_id, e)
        await message.answer(f"⚠️ Не удалось отправить сообщение: {e}", reply_markup=admin_menu_kb())


# ============================================================
#  Рассылка всем пользователям
# ============================================================

@router.callback_query(F.data == "admin_broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.message.answer(
        "Введите текст сообщения, которое будет разослано всем (не забаненным) пользователям базы.",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_broadcast_message, F.text)
async def broadcast_get_text(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text)
    await state.set_state(AdminStates.waiting_for_broadcast_confirm)

    targets = [uid for uid in user_storage.get_all() if not user_storage.is_banned(uid)]
    await message.answer(
        f"Разослать это сообщение <b>{len(targets)}</b> пользователям?\n\n{message.text}",
        reply_markup=confirm_broadcast_kb(),
    )


@router.callback_query(AdminStates.waiting_for_broadcast_confirm, F.data == "admin_broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    raw_text = data.get("broadcast_text", "")
    text = markdown_to_html(raw_text)
    await state.clear()
    await callback.answer()

    targets = [uid for uid in user_storage.get_all() if not user_storage.is_banned(uid)]
    total = len(targets)
    await callback.message.answer(f"🚀 Начинаю рассылку для {total} пользователей...")

    sent = 0
    failed = 0
    blocked_ids = []

    for user_id in targets:
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except TelegramForbiddenError:
            failed += 1
            blocked_ids.append(user_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(user_id, text)
                sent += 1
            except Exception as e2:
                logger.error("Повторная отправка пользователю %s не удалась: %s", user_id, e2)
                failed += 1
        except Exception as e:
            logger.error("Ошибка рассылки пользователю %s: %s", user_id, e)
            failed += 1

        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)

    for user_id in blocked_ids:
        await user_storage.remove(user_id)

    await callback.message.answer(
        f"✅ Рассылка завершена.\n"
        f"Отправлено: {sent}\n"
        f"Не доставлено: {failed}"
        + (f"\n(удалено из базы как заблокировавших бота: {len(blocked_ids)})" if blocked_ids else ""),
        reply_markup=admin_menu_kb(),
    )
    logger.info(
        "Админ %s разослал сообщение %d пользователям (успешно: %d, ошибок: %d)",
        callback.from_user.id, total, sent, failed,
    )


# ============================================================
#  Бан / разбан
# ============================================================

@router.callback_query(F.data == "admin_ban_user")
async def ban_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_ban_user_id)
    await callback.message.answer(
        "Введите Telegram ID пользователя, которого нужно забанить.",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_ban_user_id, F.text)
async def ban_user_finish(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await state.clear()

    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ ID должен быть числом.", reply_markup=admin_menu_kb())
        return

    user_id = int(text)
    if user_id == config.ADMIN_ID:
        await message.answer("⚠️ Нельзя забанить самого себя.", reply_markup=admin_menu_kb())
        return

    ok = await user_storage.ban(user_id)
    if ok:
        await message.answer(f"🚫 Пользователь {user_id} забанен.", reply_markup=admin_menu_kb())
        logger.info("Админ %s забанил пользователя %s", message.from_user.id, user_id)
    else:
        await message.answer("Пользователь с таким ID не найден в базе.", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "admin_unban_user")
async def unban_user_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_unban_user_id)
    await callback.message.answer(
        "Введите Telegram ID пользователя, которого нужно разбанить.",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_unban_user_id, F.text)
async def unban_user_finish(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    await state.clear()

    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ ID должен быть числом.", reply_markup=admin_menu_kb())
        return

    user_id = int(text)
    ok = await user_storage.unban(user_id)
    if ok:
        await message.answer(f"✅ Пользователь {user_id} разбанен.", reply_markup=admin_menu_kb())
        logger.info("Админ %s разбанил пользователя %s", message.from_user.id, user_id)
    else:
        await message.answer("Пользователь с таким ID не найден в базе.", reply_markup=admin_menu_kb())


# ============================================================
#  Fallback: поиск пользователя по ID в любом состоянии
# ============================================================

@router.message(F.text.regexp(r"^-?\d+$"))
async def fallback_lookup(message: Message, state: FSMContext) -> None:
    """Если админ ввёл числовой ID пользователя из базы — показать карточку.

    Фильтр совпадает ТОЛЬКО на чисто числовое сообщение (ID пользователя),
    чтобы НЕ перехватывать обычные текстовые сообщения и не забирать их
    у user.handle_text (иначе бот молчит на любые обычные тексты в ЛС).
    """
    if state is None:
        return
    current = await state.get_state()
    if current is not None:
        return

    text = message.text.strip()
    if not text.lstrip("-").isdigit():
        return

    user_id = int(text)
    if not user_storage.contains(user_id):
        await message.answer("Пользователь с таким ID не найден в базе.", reply_markup=admin_menu_kb())
        return

    record = user_storage.get_record(user_id)
    profile_text = record.get("profile") or "— пока нет данных —"
    status = "🚫 забанен" if record.get("banned") else "активен"
    username = f"@{record['username']}" if record.get("username") else "—"

    model_key = user_settings.get_model(user_id)
    model_label = "⚡ Быстрая" if model_key == "fast" else "🧠 Думающая"
    custom_prompt = user_settings.get_system_prompt(user_id)
    custom_prompt_display = f"<code>{custom_prompt}</code>" if custom_prompt else "— не задан —"

    await message.answer(
        f"👤 <b>Пользователь {user_id}</b>\n"
        f"Имя: {record.get('full_name') or '—'}\n"
        f"Username: {username}\n"
        f"Статус: {status}\n"
        f"Сообщений отправлено: {record.get('message_count', 0)}\n"
        f"Первое обращение: {record.get('first_seen', '—')}\n"
        f"Последняя активность: {record.get('last_seen', '—')}\n\n"
        f"⚙️ Модель: {model_label}\n"
        f"🧠 Пользовательский промпт:\n{custom_prompt_display}\n\n"
        f"📝 Заметка ИИ о пользователе (интересы/факты):\n{profile_text}",
        reply_markup=admin_menu_kb(),
    )


# ============================================================
#  Экспорт истории чата через юзербота
# ============================================================
# Bot API не даёт ботам историю группы до вступления. Для этого используем
# личный Telegram-аккаунт (Telethon-сессия). Запуск — вручную (разовый экспорт).

def _userbot_configured() -> bool:
    return bool(config.USERBOT_API_ID and config.USERBOT_API_HASH)


async def _run_userbot_export(chat: str, user: str | None, limit: int, msg: Message) -> None:
    """Запускает userbot_export.py как подпроцесс и сообщает результат админу."""
    import os
    import sys

    env = dict(os.environ)
    cmd = [sys.executable, "userbot_export.py", "--chat", chat, "--limit", str(limit)]
    if user:
        cmd += ["--user", user]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.path.dirname(os.path.abspath(__file__)) + "/..",
        env=env,
    )
    stdout, stderr = await proc.communicate()
    out = (stdout + stderr).decode("utf-8", errors="replace").strip()

    if proc.returncode == 0:
        await msg.answer(f"\u2705 Экспорт завершён.\n<pre>{out[-1000:]}</pre>")
    else:
        await msg.answer(
            f"\u274c Экспорт не удался (код {proc.returncode}).\n<pre>{out[-1000:]}</pre>"
        )


@router.message(Command("export_chat"))
async def cmd_export_chat(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/export_chat &lt;chat_id|@chat&gt; [limit]</code>\n"
            "Пример: <code>/export_chat -100123456789 5000</code>"
        )
        return
    if not _userbot_configured():
        await message.answer(
            "Юзербот не настроен. Задайте USERBOT_API_ID и USERBOT_API_HASH в .env."
        )
        return
    chat = parts[1]
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 2000
    await message.answer(f"⏳ Запускаю экспорт чата {chat} (до {limit} сообщений)…")
    asyncio.create_task(_run_userbot_export(chat, None, limit, message))


@router.message(Command("export_user"))
async def cmd_export_user(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Использование: <code>/export_user &lt;chat_id|@chat&gt; &lt;user_id|@username&gt; [limit]</code>"
        )
        return
    if not _userbot_configured():
        await message.answer(
            "Юзербот не настроен. Задайте USERBOT_API_ID и USERBOT_API_HASH в .env."
        )
        return
    chat = parts[1]
    user = parts[2]
    limit = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 500
    await message.answer(f"⏳ Запускаю экспорт участника {user} из чата {chat} (до {limit})…")
    asyncio.create_task(_run_userbot_export(chat, user, limit, message))


@router.message(Command("loadchat"))
async def cmd_loadchat(message: Message) -> None:
    """Импортировать файлы экспорта чатов из папки IMPORT_EXPORT_DIR (/app/data)."""
    from chat_import import import_export_dir

    directory = config.IMPORT_EXPORT_DIR
    await message.answer(f"⏳ Сканирую папку <code>{directory}</code>…")

    summary = await asyncio.to_thread(import_export_dir, directory)

    lines = [f"📁 {directory}"]
    lines.append(f"Импортировано файлов: <b>{summary['imported']}</b>")
    if summary["files"]:
        lines.append("")
        for fname, info in summary["files"].items():
            lines.append(f"• {fname}: чат <code>{info['chat_id']}</code>, {info['messages']} сообщений")
    if summary["errors"]:
        lines.append("")
        lines.append("Ошибки:")
        for err in summary["errors"][:20]:
            lines.append(f"⚠️ {err}")

    await message.answer("\n".join(lines))


@router.message(Command("learn"))
async def cmd_learn(message: Message) -> None:
    """Собрать/обновить базу знаний об участниках из экспортированной истории.

    Строит портреты активных участников и собирает локальные кликухи чатов,
    чтобы бот знал имена и понимал местные мемы при ответах в группах.
    """
    import member_knowledge
    await message.answer("⏳ Изучаю историю чатов и строю портреты участников…")
    try:
        knowledge = await member_knowledge.build_member_knowledge()
    except Exception as e:
        logger.error("Ошибка при построении базы знаний: %s", e)
        await message.answer(f"⚠️ Не удалось построить базу знаний: {e}")
        return

    total = len(knowledge)
    lines = [f"✅ База знаний обновлена. Участников: <b>{total}</b>"]
    # Топ-участников по числу сообщений.
    ranked = sorted(
        (rec for rec in knowledge.values()),
        key=lambda r: r.get("message_count", 0),
        reverse=True,
    )
    for rec in ranked[:10]:
        name = rec.get("name") or "?"
        cnt = rec.get("message_count", 0)
        lines.append(f"• {name}: {cnt} сообщений")
    await message.answer("\n".join(lines))


@router.message(Command("reload_members"))
async def cmd_reload_members(message: Message) -> None:
    """Перечитать экспорт чата из IMPORT_EXPORT_DIR (новый result.json)
    и ПРИНУДИТЕЛЬНО обновить портреты уже известных участников.

    Сначала импортирует файлы экспорта из папки (/app/data), затем пересобирает
    базу знаний, игнорируя кэш давности (force=True) — чтобы новая информация
    по известным пользователям сразу попала в ответы.
    """
    import member_knowledge
    from chat_import import import_export_dir

    await message.answer("⏳ Перечитываю экспорт чата из <code>/app/data</code>…")

    summary = await asyncio.to_thread(import_export_dir, config.IMPORT_EXPORT_DIR)

    await message.answer(
        f"✅ Экспорт прочитан: файлов <b>{summary['imported']}</b>, ошибок "
        f"<b>{len(summary['errors'])}</b>.\n⏳ Обновляю информацию по уже известным "
        f"участникам (это может занять время)…"
    )

    try:
        knowledge = await member_knowledge.build_member_knowledge(force=True)
    except Exception as e:
        logger.error("Ошибка при принудительном обновлении знаний: %s", e)
        await message.answer(f"⚠️ Не удалось обновить базу знаний: {e}")
        return

    total = len(knowledge)
    lines = [f"✅ База знаний пересобрана. Участников: <b>{total}</b>"]
    ranked = sorted(
        (rec for rec in knowledge.values()),
        key=lambda r: r.get("message_count", 0),
        reverse=True,
    )
    for rec in ranked[:10]:
        name = rec.get("name") or "?"
        cnt = rec.get("message_count", 0)
        lines.append(f"• {name}: {cnt} сообщений")
    await message.answer("\n".join(lines))
