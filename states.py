from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_for_new_prompt = State()

    waiting_for_direct_user_id = State()
    waiting_for_direct_message = State()

    waiting_for_broadcast_message = State()
    waiting_for_broadcast_confirm = State()

    waiting_for_model_name = State()

    waiting_for_ban_user_id = State()
    waiting_for_unban_user_id = State()

    waiting_for_profile_user_id = State()

    waiting_for_view_messages_user_id = State()

    waiting_for_gen_param_value = State()

    waiting_for_chat_sysprompt_chatid_set = State()
    waiting_for_chat_sysprompt_text = State()
    waiting_for_chat_sysprompt_chatid_show = State()
    waiting_for_chat_sysprompt_chatid_clear = State()


class UserStates(StatesGroup):
    waiting_for_user_prompt = State()
