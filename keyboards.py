from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Показать текущий промт", callback_data="admin_show_prompt")],
            [InlineKeyboardButton(text="✏️ Изменить промт", callback_data="admin_edit_prompt")],
            [InlineKeyboardButton(text="🔄 Сбросить промт", callback_data="admin_reset_prompt")],
            [InlineKeyboardButton(text="🤖 Сменить модель DeepSeek", callback_data="admin_change_model")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="👤 Профиль пользователя", callback_data="admin_view_profile")],
            [InlineKeyboardButton(text="✉️ Написать пользователю", callback_data="admin_direct_message")],
            [InlineKeyboardButton(text="📢 Рассылка всем", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🚫 Забанить пользователя", callback_data="admin_ban_user")],
            [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="admin_unban_user")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")],
        ]
    )


def confirm_broadcast_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить всем", callback_data="admin_broadcast_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")],
        ]
    )


def chats_list_kb(chats: list) -> InlineKeyboardMarkup:
    """Список чатов пользователя: кнопка переключения + кнопка удаления в каждой строке."""
    rows = []
    for chat in chats:
        label = ("✅ " if chat["active"] else "") + chat["name"]
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"chat_switch:{chat['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"chat_delete:{chat['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="🆕 Новый чат", callback_data="chat_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_placeholder_kb() -> InlineKeyboardMarkup:
    """
    Пустая на вид клавиатура, приклеенная к inline-результату. Без неё
    Telegram не пришлёт inline_message_id в chosen_inline_result, и бот не
    сможет отредактировать сообщение (показать анимацию/финальный ответ).
    """
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏳", callback_data="noop")]])
