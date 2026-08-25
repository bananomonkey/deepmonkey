from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from typing import Union

import config


class IsAdmin(BaseFilter):
    """Пропускает событие, только если его отправитель — администратор (config.ADMIN_ID)."""

    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        return event.from_user is not None and event.from_user.id == config.ADMIN_ID
