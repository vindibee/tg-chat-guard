"""Проверка новых участников по внешним блок-листам при входе в чат."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.chat_member_updated import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from bot.services.reputation_service import ReputationService
from bot.services.whitelist_service import WhitelistService
from bot.utils.telegram import safe_ban_member

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

logger = logging.getLogger(__name__)

async def check_new_member(
    event: ChatMemberUpdated,
    bot: Bot,
    settings: Settings,
    reputation: ReputationService,
    whitelist: WhitelistService,
    **_: Any,
) -> None:
    """Сверяет вошедшего с реестрами спамеров до его первого сообщения.

    По умолчанию бот не банит, а зовёт админов: публичные базы ошибаются,
    и цена ложного бана на входе выше, чем цена одного лишнего уведомления.
    """
    user = event.new_chat_member.user
    chat = event.chat
    if user.is_bot or not reputation.enabled:
        return

    username = (user.username or "").lower() or None
    if await whitelist.is_trusted(chat.id, user.id, username):
        return

    verdict = await reputation.check(user.id, online=True)
    if not verdict.banned:
        return

    banned = False
    if settings.reputation_autoban:
        banned = await safe_ban_member(bot, chat.id, user.id)

    logger.warning(
        "Новый участник найден в блок-листе",
        extra={
            "component": "join_guard",
            "chat_id": chat.id,
            "user_id": user.id,
            "username": user.username,
            "blocklist": verdict.source,
            "autobanned": banned,
        },
    )
    await _alert_admins(bot, settings, event, verdict.source, verdict.when, banned)


async def _alert_admins(
    bot: Bot,
    settings: Settings,
    event: ChatMemberUpdated,
    source: str,
    when: str | None,
    banned: bool,
) -> None:
    """Сообщает в админ-чат о подозрительном новичке."""
    if settings.admin_log_chat_id is None:
        return

    user = event.new_chat_member.user
    name = html.escape(user.full_name)
    handle = f" @{html.escape(user.username)}" if user.username else ""
    status = "⛔️ забанен автоматически" if banned else "⚠️ мер не принято — решайте сами"
    seen = f"\nВ базе с: {html.escape(when)}" if when else ""

    text = (
        f"🚩 <b>Новичок из блок-листа</b>\n"
        f'Чат: {html.escape(event.chat.title or str(event.chat.id))}\n'
        f'Участник: <a href="tg://user?id={user.id}">{name}</a>{handle} · '
        f"<code>{user.id}</code>\n"
        f"Источник: <code>{html.escape(source or 'unknown')}</code>{seen}\n"
        f"Статус: {status}"
    )
    try:
        await bot.send_message(settings.admin_log_chat_id, text, disable_web_page_preview=True)
    except TelegramAPIError as exc:
        logger.warning("Не удалось предупредить админов о новичке: %s", exc)


def build_router() -> Router:
    """Роутер проверки новичков на входе."""
    router = Router(name="join_guard")
    router.chat_member.register(check_new_member, ChatMemberUpdatedFilter(JOIN_TRANSITION))
    return router
