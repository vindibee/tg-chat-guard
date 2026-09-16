"""Уборка «мусорных» ответов книжного бота.

Сам бот Флибусты неприкосновенен: его сообщения не проверяются антиспамом,
ему не выдаются санкции и не начисляются баллы. Но два вида его ответов в
чате не нужны:

1. **«Ничего не найдено»** — служебный отказ на неудачный запрос;
2. **ответ в пустоту** — пользовательский запрос уже удалён антиспамом,
   а цитата с ответом осталась висеть.

Второй случай определяется по реестру удалённых сообщений: Telegram не
сообщает ботам об удалении, поэтому бот помнит собственные удаления сам.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.services.deleted_registry import DeletedMessageRegistry
from bot.services.stopwords import compile_alternation
from bot.services.text_cleaner import TextCleaner
from bot.utils.telegram import safe_delete_message

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["clean_flibusta_reply", "detect_junk", "build_router", "JUNK_RE"]

#: Ответы-отказы книжного бота. Паттерны нормализуются так же, как текст
#: сообщения, поэтому регистр и «ё» значения не имеют.
JUNK_RE: Final = compile_alternation(
    (
        r"не наидено книг",
        r"ничего не наидено",
        r"книг не наидено",
        r"по запросу.{0,40}(ничего )?не наидено",
        r"поиск не дал результат",
        r"результатов не наидено",
    )
)

#: Крестик в ответе на запрос — тоже отказ, но формулировка бывает разной.
_FAIL_MARK: Final[str] = "❌"


def detect_junk(
    message: Message, deleted_registry: DeletedMessageRegistry
) -> str | None:
    """Определяет, нужно ли убирать ответ книжного бота.

    Returns:
        Причина удаления (`not_found` / `failed_search` / `orphan_reply`)
        либо `None`, если ответ полезный.
    """
    text = message.text or message.caption or ""
    normalized = TextCleaner.normalize(text)

    if normalized.search(JUNK_RE) is not None:
        return "not_found"

    # «По запросу … ❌» — отказ без слова «не найдено».
    if _FAIL_MARK in text and "по запросу" in normalized.lowered:
        return "failed_search"

    reply = message.reply_to_message
    if reply is not None and deleted_registry.was_deleted(message.chat.id, reply.message_id):
        return "orphan_reply"

    return None


async def clean_flibusta_reply(
    message: Message,
    settings: Settings,
    deleted_registry: DeletedMessageRegistry,
    **_: Any,
) -> None:
    """Убирает ответ книжного бота, если он бесполезен."""
    if not settings.flibusta_cleanup:
        return

    reason = detect_junk(message, deleted_registry)
    if reason is None:
        return

    if settings.dry_run:
        logger.info(
            "DRY-RUN: ответ книжного бота оставлен",
            extra={"component": "flibusta", "chat_id": message.chat.id, "reason": reason},
        )
        return

    deleted = await safe_delete_message(message)
    logger.info(
        "Убран ответ книжного бота",
        extra={
            "component": "flibusta",
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "reason": reason,
            "deleted": deleted,
        },
    )


def build_router(settings: Settings) -> Router:
    """Роутер работает только при известном id книжного бота."""
    router = Router(name="flibusta_filter")
    if settings.flibusta_bot_id is None or not settings.flibusta_cleanup:
        logger.info(
            "Фильтр ответов книжного бота выключен",
            extra={"stage": "startup", "flibusta_bot_id": settings.flibusta_bot_id},
        )
        return router

    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    router.message.register(
        clean_flibusta_reply, F.from_user.id == settings.flibusta_bot_id
    )
    logger.info(
        "Фильтр ответов книжного бота включён",
        extra={"stage": "startup", "flibusta_bot_id": settings.flibusta_bot_id},
    )
    return router
