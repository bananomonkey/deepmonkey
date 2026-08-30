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


class UserStates(StatesGroup):
    waiting_for_user_prompt = State()
