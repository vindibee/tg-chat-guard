"""Безопасные обёртки над Bot API и разбор содержимого сообщений."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import ChatPermissions, Message

logger = logging.getLogger(__name__)

__all__ = [
    "safe_delete_message",
    "safe_restrict_member",
    "safe_ban_member",
    "safe_ban_sender_chat",
    "safe_unrestrict_member",
    "safe_unban_member",
    "extract_payload",
    "MUTED_PERMISSIONS",
    "DEFAULT_PERMISSIONS",
]

#: Полностью «немой» участник: не может отправлять ничего.
MUTED_PERMISSIONS: ChatPermissions = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)


async def safe_delete_message(message: Message, *, retries: int = 1) -> bool:
    """Удаляет сообщение, не падая на типовых ошибках Telegram.

    Returns:
        True, если сообщение удалено.
    """
    for attempt in range(retries + 1):
        try:
            await message.delete()
            return True
        except TelegramRetryAfter as exc:
            if attempt >= retries:
                logger.warning("Лимит Telegram при удалении: %s", exc)
                return False
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest as exc:
            # message to delete not found / message can't be deleted / too old
            logger.info(
                "Сообщение удалить нельзя: %s",
                exc.message,
                extra={
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "component": "telegram",
                },
            )
            return False
        except TelegramForbiddenError as exc:
            logger.warning(
                "Нет прав на удаление сообщений: %s",
                exc.message,
                extra={"chat_id": message.chat.id, "component": "telegram"},
            )
            return False
    return False


#: Разумный набор прав, если настройки чата получить не удалось.
DEFAULT_PERMISSIONS: ChatPermissions = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def safe_restrict_member(
    bot: Bot, chat_id: int, user_id: int, seconds: int
) -> bool:
    """Выдаёт mute на `seconds`. Возвращает успех операции."""
    until = datetime.now(tz=UTC) + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=MUTED_PERMISSIONS,
            until_date=until,
        )
        return True
    except TelegramRetryAfter as exc:
        logger.warning("Лимит Telegram при mute: %s", exc)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "Не удалось ограничить участника: %s",
            exc.message,
            extra={"chat_id": chat_id, "user_id": user_id, "component": "telegram"},
        )
    return False


async def safe_ban_member(
    bot: Bot, chat_id: int, user_id: int, *, revoke_messages: bool = True
) -> bool:
    """Банит участника. Возвращает успех операции."""
    try:
        await bot.ban_chat_member(
            chat_id=chat_id, user_id=user_id, revoke_messages=revoke_messages
        )
        return True
    except TelegramRetryAfter as exc:
        logger.warning("Лимит Telegram при бане: %s", exc)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "Не удалось забанить участника: %s",
            exc.message,
            extra={"chat_id": chat_id, "user_id": user_id, "component": "telegram"},
        )
    return False


def extract_payload(message: Message) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Достаёт из сообщения текст, скрытые URL и упоминания.

    Returns:
        `(текст, url из сущностей, упоминания без «@»)`.
    """
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []

    urls: list[str] = []
    mentions: list[str] = []
    for entity in entities:
        if entity.type == "text_link" and entity.url:
            urls.append(entity.url.strip())
        elif entity.type == "url":
            # Смещения приходят в единицах UTF-16; `extract_from` это учитывает,
            # но на битых сущностях возвращает пустую строку — её нельзя пускать
            # дальше, иначе движок засчитает её как скрытую ссылку.
            urls.append(entity.extract_from(text).strip())
        elif entity.type == "mention":
            mentions.append(entity.extract_from(text).lstrip("@").strip().lower())
        elif entity.type == "text_mention" and entity.user is not None:
            if entity.user.username:
                mentions.append(entity.user.username.lower())
    return (
        text,
        tuple(url for url in dict.fromkeys(urls) if url),
        tuple(name for name in dict.fromkeys(mentions) if name),
    )


async def safe_ban_sender_chat(bot: Bot, chat_id: int, sender_chat_id: int) -> bool:
    """Блокирует канал, от имени которого пишут спам.

    У таких сообщений нет автора-пользователя, поэтому обычный бан к ним
    неприменим — Telegram предлагает для этого отдельный метод.
    """
    try:
        await bot.ban_chat_sender_chat(chat_id=chat_id, sender_chat_id=sender_chat_id)
        return True
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
        logger.warning(
            "Не удалось заблокировать канал-отправитель: %s",
            getattr(exc, "message", exc),
            extra={
                "chat_id": chat_id,
                "sender_chat_id": sender_chat_id,
                "component": "telegram",
            },
        )
        return False


async def safe_unrestrict_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Снимает mute, возвращая участнику права чата по умолчанию."""
    permissions = DEFAULT_PERMISSIONS
    try:
        chat = await bot.get_chat(chat_id)
        if chat.permissions is not None:
            permissions = chat.permissions
    except TelegramAPIError as exc:
        logger.info("Не удалось прочитать права чата, ставим набор по умолчанию: %s", exc)

    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id, permissions=permissions
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
        logger.warning(
            "Не удалось снять ограничение: %s",
            getattr(exc, "message", exc),
            extra={"chat_id": chat_id, "user_id": user_id, "component": "telegram"},
        )
        return False


async def safe_unban_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Снимает бан. `only_if_banned` не даёт случайно кикнуть участника."""
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        return True
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter) as exc:
        logger.warning(
            "Не удалось снять бан: %s",
            getattr(exc, "message", exc),
            extra={"chat_id": chat_id, "user_id": user_id, "component": "telegram"},
        )
        return False
