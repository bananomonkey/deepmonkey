from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from user_settings import user_settings


def admin_menu_kb() -> InlineKeyboardMarkup:
    thinking_label = (
        "🧠 Думающая модель: ВКЛ ✅" if user_settings.is_thinking_enabled()
        else "🧠 Думающая модель: ВЫКЛ ⛔"
    )
    multimodal_label = (
        "👁 Мультимодальность: ВКЛ ✅" if user_settings.is_multimodal_enabled()
        else "👁 Мультимодальность: ВЫКЛ ⛔"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Показать текущий промт", callback_data="admin_show_prompt")],
            [InlineKeyboardButton(text="✏️ Изменить промт", callback_data="admin_edit_prompt")],
            [InlineKeyboardButton(text="🔄 Сбросить промт", callback_data="admin_reset_prompt")],
            [InlineKeyboardButton(text="🤖 Сменить модель DeepSeek", callback_data="admin_change_model")],
            [InlineKeyboardButton(text="🎛 Параметры генерации", callback_data="admin_gen_params")],
            [InlineKeyboardButton(text=thinking_label, callback_data="admin_toggle_thinking")],
            [InlineKeyboardButton(text=multimodal_label, callback_data="admin_toggle_multimodal")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="👥 Пользователи в базе", callback_data="admin_view_users")],
            [InlineKeyboardButton(text="💬 Сообщения пользователя в ЛС", callback_data="admin_view_user_messages")],
            [InlineKeyboardButton(text="✉️ Написать пользователю", callback_data="admin_direct_message")],
            [InlineKeyboardButton(text="📢 Рассылка всем", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🚫 Забанить пользователя", callback_data="admin_ban_user")],
            [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="admin_unban_user")],
        ]
    )


def gen_params_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌡 Температура", callback_data="gen_param:temperature")],
            [InlineKeyboardButton(text="🎯 Top-P", callback_data="gen_param:top_p")],
            [InlineKeyboardButton(text="🔢 Max токенов", callback_data="gen_param:max_tokens")],
            [InlineKeyboardButton(text="⛔ Штраф повторов", callback_data="gen_param:frequency_penalty")],
            [InlineKeyboardButton(text="✨ Штраф новизны", callback_data="gen_param:presence_penalty")],
            [InlineKeyboardButton(text="🔄 Сбросить все", callback_data="gen_param:reset_all")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_cancel")],
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
        if chat.get("preview"):
            label += "\n" + chat["preview"]
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"chat_switch:{chat['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"chat_delete:{chat['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="🆕 Новый чат", callback_data="chat_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def model_select_kb(current_model: str) -> InlineKeyboardMarkup:
    fast_label = "⚡ Быстрая" + (" ✅" if current_model == "fast" else "")
    think_label = "🧠 Думающая" + (" ✅" if current_model == "thinking" else "")
    vision_label = "👁 Vision (видит фото)" + (" ✅" if current_model == "vision" else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=fast_label, callback_data="set_model:fast")],
            [InlineKeyboardButton(text=think_label, callback_data="set_model:thinking")],
            [InlineKeyboardButton(text=vision_label, callback_data="set_model:vision")],
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


def inline_placeholder_kb() -> InlineKeyboardMarkup:
    """
    Клавиатура-заглушка для классического inline-результата. БЕЗ неё Telegram
    не пришлёт inline_message_id в chosen_inline_result (см. официальный Bot
    API: 'inline_message_id: Available only if there is an inline keyboard
    attached to the message'), и бот не сможет отредактировать сообщение.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏳", callback_data="noop")]])
