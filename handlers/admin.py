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
#  Профиль пользователя
# ============================================================

@router.callback_query(F.data == "admin_view_profile")
async def view_profile_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_profile_user_id)
    await callback.message.answer(
        "Введите Telegram ID пользователя, чей профиль хотите посмотреть.",
        reply_markup=cancel_kb(),
    )
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

    await message.answer(
        f"👤 <b>Пользователь {user_id}</b>\n"
        f"Имя: {record.get('full_name') or '—'}\n"
        f"Username: {username}\n"
        f"Статус: {status}\n"
        f"Сообщений отправлено: {record.get('message_count', 0)}\n"
        f"Первое обращение: {record.get('first_seen', '—')}\n"
        f"Последняя активность: {record.get('last_seen', '—')}\n\n"
        f"📝 Заметка ИИ о пользователе (интересы/факты):\n{profile_text}",
        reply_markup=admin_menu_kb(),
    )
    logger.info("Администратор %s посмотрел профиль пользователя %s", message.from_user.id, user_id)


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
