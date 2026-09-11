"""Карточка модерации в админ-чате и разбор срабатываний.

Зачем: без внешнего канала обратной связи ложные срабатывания невидимы —
участник просто молча уходит из чата. Каждое срабатывание дублируется сюда
с кнопками отката, а решения админа попадают в журнал.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any, Final

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db.models import ModerationEvent
from bot.services.event_service import EventService
from bot.services.whitelist_service import WhitelistError, WhitelistService
from bot.utils.telegram import safe_unban_member, safe_unrestrict_member

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings
    from bot.services.admin_cache import ChatAdminCache

logger = logging.getLogger(__name__)

#: Подписи санкций для карточки.
_ACTION_TITLES: Final[dict[str, str]] = {
    "delete": "🚫 Сообщение удалено",
    "delete_and_mute": "🔇 Удалено + мьют",
    "delete_and_ban": "⛔️ Удалено + бан",
}


class ReviewCallback(CallbackData, prefix="rv"):
    """Данные кнопок карточки: что сделать с событием `event_id`."""

    action: str  # fp | ok | wl
    event_id: int


def build_keyboard(event_id: int, *, with_undo: bool = True) -> InlineKeyboardMarkup:
    """Кнопки карточки. После отката кнопка «Не спам» убирается."""
    rows: list[list[InlineKeyboardButton]] = []
    if with_undo:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Не спам",
                    callback_data=ReviewCallback(action="fp", event_id=event_id).pack(),
                ),
                InlineKeyboardButton(
                    text="✔️ Верно",
                    callback_data=ReviewCallback(action="ok", event_id=event_id).pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ В белый список",
                callback_data=ReviewCallback(action="wl", event_id=event_id).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_card(event: ModerationEvent, message: Message, *, dry_run: bool) -> str:
    """Собирает текст карточки для админ-чата."""
    user = message.from_user
    title = _ACTION_TITLES.get(event.action, "🚫 Срабатывание")
    if dry_run:
        title = f"👁 Наблюдение ({title.split(' ', 1)[1]})"

    if user is not None:
        name = html.escape(user.full_name)
        author = f'<a href="tg://user?id={user.id}">{name}</a>'
        if user.username:
            author += f" @{html.escape(user.username)}"
        author += f" · <code>{user.id}</code>"
    else:
        author = "— (от имени чата)"

    chat_title = html.escape(message.chat.title or str(message.chat.id))
    categories = event.categories or "—"
    excerpt = html.escape(event.excerpt[:600]) or "—"

    return (
        f"{title} · <b>скор {event.score:.1f}</b>\n"
        f"Чат: {chat_title}\n"
        f"Автор: {author}\n"
        f"Причина: <code>{event.reason}</code> · {html.escape(categories)}\n"
        f"Сигналы: <code>{html.escape(event.signals or '—')}</code>\n"
        f"\n<blockquote>{excerpt}</blockquote>\n"
        f"\n<code>event #{event.id}</code>"
    )


async def send_review_card(
    bot: Bot, settings: Settings, event: ModerationEvent, message: Message
) -> None:
    """Отправляет карточку в админ-чат. Молча пропускает, если чат не настроен."""
    if settings.admin_log_chat_id is None:
        return
    try:
        await bot.send_message(
            chat_id=settings.admin_log_chat_id,
            text=render_card(event, message, dry_run=settings.dry_run),
            reply_markup=build_keyboard(event.id),
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Не удалось отправить карточку в админ-чат: %s",
            exc,
            extra={"component": "admin_review", "admin_chat": settings.admin_log_chat_id},
        )


async def _is_allowed(
    callback: CallbackQuery,
    event: ModerationEvent,
    bot: Bot,
    settings: Settings,
    admin_cache: ChatAdminCache,
) -> bool:
    """Кнопки доступны админам исходного чата и суперадминам бота."""
    user = callback.from_user
    if user.id in settings.super_admin_ids:
        return True
    return await admin_cache.is_admin(bot, event.chat_id, user.id)


async def _finish(callback: CallbackQuery, status: str, *, event_id: int, undo_done: bool) -> None:
    """Дописывает итог в карточку и перестраивает кнопки."""
    message = callback.message
    if not isinstance(message, Message):
        return
    body = message.html_text if message.text else ""
    keyboard = build_keyboard(event_id, with_undo=False) if undo_done else None
    try:
        await message.edit_text(
            f"{body}\n\n{status}", reply_markup=keyboard, disable_web_page_preview=True
        )
    except TelegramAPIError as exc:
        logger.info("Не удалось обновить карточку: %s", exc)


async def undo_false_positive(
    callback: CallbackQuery,
    callback_data: ReviewCallback,
    bot: Bot,
    settings: Settings,
    events: EventService,
    admin_cache: ChatAdminCache,
    **_: Any,
) -> None:
    """Откат: снимаем санкцию, возвращаем текст и помечаем срабатывание ложным."""
    event = await events.get(callback_data.event_id)
    if event is None:
        await callback.answer("Событие не найдено.", show_alert=True)
        return
    if not await _is_allowed(callback, event, bot, settings, admin_cache):
        await callback.answer("Только для админов чата.", show_alert=True)
        return

    restored: list[str] = []
    if event.action == "delete_and_mute":
        if await safe_unrestrict_member(bot, event.chat_id, event.user_id):
            restored.append("мьют снят")
    elif event.action == "delete_and_ban":
        if await safe_unban_member(bot, event.chat_id, event.user_id):
            restored.append("бан снят")

    if settings.restore_on_false_positive and event.deleted and event.excerpt:
        mention = f'<a href="tg://user?id={event.user_id}">автора</a>'
        try:
            await bot.send_message(
                chat_id=event.chat_id,
                text=(
                    f"♻️ Сообщение {mention} восстановлено — антиспам ошибся.\n\n"
                    f"<blockquote>{html.escape(event.excerpt[:900])}</blockquote>"
                ),
                disable_web_page_preview=True,
            )
            restored.append("текст возвращён в чат")
        except TelegramAPIError as exc:
            logger.warning("Не удалось вернуть текст в чат: %s", exc)

    await events.mark_reviewed(event.id, admin_id=callback.from_user.id, false_positive=True)
    logger.warning(
        "Ложное срабатывание подтверждено админом",
        extra={
            "component": "admin_review",
            "event_id": event.id,
            "chat_id": event.chat_id,
            "user_id": event.user_id,
            "admin_id": callback.from_user.id,
            "verdict_score": event.score,
            "verdict_reason": event.reason,
            "excerpt": event.excerpt[:200],
        },
    )

    detail = ", ".join(restored) or "санкций не было"
    await _finish(
        callback,
        f"✅ <b>Ложное срабатывание</b> · {html.escape(callback.from_user.full_name)} · {detail}",
        event_id=event.id,
        undo_done=True,
    )
    await callback.answer("Откат выполнен.")


async def confirm_spam(
    callback: CallbackQuery,
    callback_data: ReviewCallback,
    bot: Bot,
    settings: Settings,
    events: EventService,
    admin_cache: ChatAdminCache,
    **_: Any,
) -> None:
    """Подтверждение: решение верное, карточка закрывается."""
    event = await events.get(callback_data.event_id)
    if event is None:
        await callback.answer("Событие не найдено.", show_alert=True)
        return
    if not await _is_allowed(callback, event, bot, settings, admin_cache):
        await callback.answer("Только для админов чата.", show_alert=True)
        return

    await events.mark_reviewed(event.id, admin_id=callback.from_user.id, false_positive=False)
    await _finish(
        callback,
        f"✔️ <b>Подтверждено</b> · {html.escape(callback.from_user.full_name)}",
        event_id=event.id,
        undo_done=False,
    )
    await callback.answer("Отмечено.")


async def whitelist_from_card(
    callback: CallbackQuery,
    callback_data: ReviewCallback,
    bot: Bot,
    settings: Settings,
    events: EventService,
    whitelist: WhitelistService,
    admin_cache: ChatAdminCache,
    **_: Any,
) -> None:
    """Добавляет автора в белый список чата прямо из карточки."""
    event = await events.get(callback_data.event_id)
    if event is None:
        await callback.answer("Событие не найдено.", show_alert=True)
        return
    if not await _is_allowed(callback, event, bot, settings, admin_cache):
        await callback.answer("Только для админов чата.", show_alert=True)
        return
    if not event.user_id:
        await callback.answer("У события нет автора.", show_alert=True)
        return

    try:
        await whitelist.add(
            chat_id=event.chat_id,
            user_id=event.user_id,
            username=event.username,
            added_by=callback.from_user.id,
            reason=f"откат срабатывания #{event.id}",
        )
    except WhitelistError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    target = f"@{event.username}" if event.username else f"id {event.user_id}"
    await _finish(
        callback,
        f"➕ <b>{html.escape(target)} в белом списке</b> · "
        f"{html.escape(callback.from_user.full_name)}",
        event_id=event.id,
        undo_done=False,
    )
    await callback.answer("Добавлен в белый список.")


def build_router() -> Router:
    """Роутер кнопок разбора в админ-чате."""
    router = Router(name="admin_review")
    router.callback_query.register(undo_false_positive, ReviewCallback.filter(F.action == "fp"))
    router.callback_query.register(confirm_spam, ReviewCallback.filter(F.action == "ok"))
    router.callback_query.register(whitelist_from_card, ReviewCallback.filter(F.action == "wl"))
    return router
