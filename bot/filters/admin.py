"""Фильтры прав доступа к админским командам."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter
from aiogram.types import Message

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings
    from bot.services.admin_cache import ChatAdminCache

__all__ = ["IsChatAdmin", "IsSuperAdmin"]


class IsSuperAdmin(BaseFilter):
    """Глобальный администратор бота (список из `.env`)."""

    async def __call__(self, message: Message, settings: Settings) -> bool:
        user = message.from_user
        return user is not None and user.id in settings.super_admin_ids


class IsChatAdmin(BaseFilter):
    """Администратор текущего чата либо суперадмин бота.

    В личных сообщениях командой могут пользоваться только суперадмины —
    иначе любой пользователь правил бы глобальным белым списком.
    """

    async def __call__(
        self,
        message: Message,
        bot: Bot,
        settings: Settings,
        admin_cache: ChatAdminCache,
    ) -> bool:
        user = message.from_user
        if user is None:
            # Анонимный админ пишет от имени чата — доверяем статусу отправителя.
            return message.sender_chat is not None and message.sender_chat.id == message.chat.id
        if user.id in settings.super_admin_ids:
            return True
        if message.chat.type == ChatType.PRIVATE:
            return False
        return await admin_cache.is_admin(bot, message.chat.id, user.id)
