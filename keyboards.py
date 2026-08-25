from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Показать текущий промт", callback_data="admin_show_prompt")],
            [InlineKeyboardButton(text="✏️ Изменить промт", callback_data="admin_edit_prompt")],
            [InlineKeyboardButton(text="🔄 Сбросить промт", callback_data="admin_reset_prompt")],
            [InlineKeyboardButton(text="🤖 Сменить модель DeepSeek", callback_data="admin_change_model")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="👥 Пользователи в базе", callback_data="admin_view_users")],
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
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏳", callback_data="noop")]])


def model_select_kb(current_model: str) -> InlineKeyboardMarkup:
    fast_label = "⚡ Быстрая" + (" ✅" if current_model == "fast" else "")
    think_label = "🧠 Думающая" + (" ✅" if current_model == "thinking" else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=fast_label, callback_data="set_model:fast")],
            [InlineKeyboardButton(text=think_label, callback_data="set_model:thinking")],
        ]
    )


def user_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Текущий промпт", callback_data="user_show_prompt")],
            [InlineKeyboardButton(text="✏️ Изменить промпт", callback_data="user_edit_prompt")],
            [InlineKeyboardButton(text="🔄 Сбросить промпт", callback_data="user_reset_prompt")],
        ]
    )


def user_prompt_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="user_prompt_cancel")],
        ]
    )
