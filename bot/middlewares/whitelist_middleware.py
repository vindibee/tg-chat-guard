"""Middleware доверия: доверенные участники не доходят до модерации."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import Message

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings
    from bot.services.activity_tracker import ActivityTracker
    from bot.services.admin_cache import ChatAdminCache
    from bot.services.whitelist_service import WhitelistService

logger = logging.getLogger(__name__)

__all__ = ["WhitelistMiddleware", "TrustReason"]


class TrustReason(StrEnum):
    """Почему сообщение не проверялось антиспамом."""

    SELF = "self"
    PRIVATE_CHAT = "private_chat"
    ANONYMOUS_ADMIN = "anonymous_admin"
    FLIBUSTA = "flibusta_bot"
    STATIC_CONFIG = "static_config"
    CHAT_ADMIN = "chat_admin"
    DYNAMIC_WHITELIST = "dynamic_whitelist"
    APPROVED_MEMBER = "approved_member"


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает сообщения доверенных отправителей мимо модерации.

    Middleware вешается на роутер модерации как внутренний (`router.message.middleware`),
    поэтому «остановка» цепочки затрагивает только антиспам — админские команды
    в других роутерах продолжают работать.
    """

    def __init__(
        self,
        settings: Settings,
        whitelist: WhitelistService,
        admin_cache: ChatAdminCache,
        activity: ActivityTracker,
    ) -> None:
        self._settings = settings
        self._whitelist = whitelist
        self._admin_cache = admin_cache
        self._activity = activity

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        data["message_count"] = 0
        reason = await self._resolve_trust(event, data)
        if reason is not None:
            logger.debug(
                "Сообщение пропущено без проверки",
                extra={
                    "component": "whitelist_mw",
                    "chat_id": event.chat.id,
                    "user_id": event.from_user.id if event.from_user else 0,
                    "trust_reason": reason.value,
                    "message_count": data.get("message_count", 0),
                },
            )
            data["trust_reason"] = reason
            return None  # цепочка модерации прерывается

        data["trust_reason"] = None
        return await handler(event, data)

    def _resolve_sender_chat(self, message: Message) -> TrustReason | None:
        """Разбирает сообщения без автора-пользователя.

        Их три вида, и доверять можно только двум:

        * анонимный администратор — `sender_chat` совпадает с самим чатом;
        * автопост привязанного канала — помечен `is_automatic_forward`;
        * сообщение от имени **постороннего** канала — классический спам-вектор,
          его обязательно проверяем.
        """
        sender_chat = message.sender_chat
        if sender_chat is None:
            return None
        anonymous_admin = sender_chat.id == message.chat.id
        linked_channel = bool(message.is_automatic_forward)
        if (anonymous_admin or linked_channel) and self._settings.trust_anonymous_admins:
            return TrustReason.ANONYMOUS_ADMIN
        return None

    async def _resolve_trust(self, message: Message, data: dict[str, Any]) -> TrustReason | None:
        """Возвращает причину доверия либо `None`, если нужна проверка."""
        chat = message.chat
        if chat.type == ChatType.PRIVATE:
            return TrustReason.PRIVATE_CHAT

        user = message.from_user
        if user is None:
            return self._resolve_sender_chat(message)

        bot: Bot | None = data.get("bot")
        if bot is not None and user.id == bot.id:
            return TrustReason.SELF

        username = (user.username or "").lower() or None

        if self._whitelist.is_flibusta(user.id, username):
            return TrustReason.FLIBUSTA
        if self._whitelist.is_statically_trusted(user.id, username):
            return TrustReason.STATIC_CONFIG

        if self._settings.trust_chat_admins and bot is not None:
            if await self._admin_cache.is_admin(bot, chat.id, user.id):
                return TrustReason.CHAT_ADMIN

        if await self._whitelist.is_trusted(chat.id, user.id, username):
            return TrustReason.DYNAMIC_WHITELIST

        # Старожил: набрал достаточно чистых сообщений — не проверяем вовсе.
        # Счётчик нужен и дальше, поэтому кладём его в data для хендлера.
        count = await self._activity.get_count(chat.id, user.id)
        data["message_count"] = count
        if self._activity.is_approved(count):
            return TrustReason.APPROVED_MEMBER

        return None
