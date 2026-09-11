"""Уборка служебных сообщений: «вступил в группу», «вышел» и прочий шум.

В живом чате эти сообщения занимают больше места, чем разговор, а пользы
не несут: кто вступил, видно по самому участнику, а кто вышел — тем более.

Что удалять, задаётся группами в `SERVICE_CLEANUP`. Платежи не удаляются
никогда, даже при `all` (см. `bot/utils/service_kinds.py`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.utils.service_kinds import resolve_content_types
from bot.utils.telegram import safe_delete_message

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["clean_service_message", "build_router"]


async def clean_service_message(message: Message, settings: Settings, **_: Any) -> None:
    """Удаляет одно служебное сообщение."""
    kind = message.content_type
    if settings.dry_run:
        logger.info(
            "DRY-RUN: служебное сообщение оставлено",
            extra={"component": "service_cleanup", "chat_id": message.chat.id, "kind": kind},
        )
        return

    deleted = await safe_delete_message(message)
    logger.debug(
        "Служебное сообщение убрано" if deleted else "Служебное сообщение удалить не вышло",
        extra={
            "component": "service_cleanup",
            "chat_id": message.chat.id,
            "kind": kind,
            "deleted": deleted,
        },
    )


def build_router(settings: Settings) -> Router:
    """Роутер уборки. При `SERVICE_CLEANUP=none` остаётся пустым."""
    router = Router(name="service_cleanup")
    content_types = resolve_content_types(settings.service_cleanup)
    if not content_types:
        logger.info("Уборка служебных сообщений отключена", extra={"stage": "startup"})
        return router

    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    router.message.register(clean_service_message, F.content_type.in_(content_types))
    logger.info(
        "Уборка служебных сообщений включена",
        extra={"stage": "startup", "service_kinds": len(content_types)},
    )
    return router
