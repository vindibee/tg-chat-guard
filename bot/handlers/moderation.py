"""Обработка нарушений: проверка сообщений, санкции и журналирование."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING, Any, Final

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from aiogram.utils.markdown import hbold

from bot.handlers.admin_review import send_review_card
from bot.services.activity_tracker import ActivityTracker
from bot.services.antispam_engine import Action, AntiSpamEngine, MessageContext, Verdict
from bot.services.deleted_registry import DeletedMessageRegistry
from bot.services.duplicate_detector import DuplicateDetector
from bot.services.event_service import EventService
from bot.services.promo_tracker import PromoTracker
from bot.services.reputation_service import ReputationService, ReputationVerdict
from bot.utils.telegram import (
    extract_payload,
    safe_ban_member,
    safe_ban_sender_chat,
    safe_delete_message,
    safe_restrict_member,
)

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

logger = logging.getLogger(__name__)

#: Держим ссылки на фоновые задачи, иначе сборщик мусора может их прервать.
_BACKGROUND_TASKS: Final[set[asyncio.Task[Any]]] = set()

#: Тексты санкций для уведомления чата.
_ACTION_LABELS: Final[dict[Action, str]] = {
    Action.DELETE: "сообщение удалено",
    Action.DELETE_AND_MUTE: "сообщение удалено, участник временно ограничен",
    Action.DELETE_AND_BAN: "сообщение удалено, участник заблокирован",
}


def _author_identity(message: Message) -> tuple[str, str]:
    """Имя и username отправителя — у пользователя либо у канала-отправителя."""
    user = message.from_user
    if user is not None:
        return user.full_name, (user.username or "").lower()
    sender_chat = message.sender_chat
    if sender_chat is not None:
        return sender_chat.title or "", (sender_chat.username or "").lower()
    return "", ""


def build_context(
    message: Message,
    settings: Settings,
    activity: ActivityTracker,
    message_count: int,
    reputation: ReputationVerdict | None = None,
    duplicate_authors: int = 0,
) -> MessageContext:
    """Собирает контекст сообщения для движка антиспама.

    Args:
        message_count: счётчик чистых сообщений автора, посчитанный в middleware.
        reputation: ответ внешних блок-листов, если проверка выполнялась.
        duplicate_authors: сколько разных участников прислали этот же текст.
    """
    text, urls, mentions = extract_payload(message)
    author_name, author_username = _author_identity(message)
    safe_usernames = set(settings.protected_usernames)
    if message.reply_to_message and message.reply_to_message.from_user:
        # Упоминание собеседника в ответе — нормальное поведение, не спам-фактор.
        replied = message.reply_to_message.from_user.username
        if replied:
            safe_usernames.add(replied.lower())
    return MessageContext(
        text=text,
        entity_urls=urls,
        mentions=mentions,
        is_forward=message.forward_origin is not None,
        forward_from_chat=getattr(message.forward_origin, "chat", None) is not None,
        has_media=bool(message.photo or message.video or message.document),
        is_new_member=activity.is_new_member(message_count),
        known_safe_usernames=frozenset(safe_usernames),
        author_name=author_name,
        author_username=author_username,
        external_ban=bool(reputation and reputation.banned),
        external_ban_source=reputation.source if reputation else "",
        duplicate_authors=duplicate_authors,
    )


async def moderate_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    engine: AntiSpamEngine,
    activity: ActivityTracker,
    events: EventService,
    reputation: ReputationService,
    duplicates: DuplicateDetector,
    deleted_registry: DeletedMessageRegistry,
    message_count: int = 0,
    promo: PromoTracker | None = None,
    **_: Any,
) -> None:
    """Главный обработчик: проверяет сообщение и применяет санкции."""
    user = message.from_user
    user_id = user.id if user else 0

    # Книжный бот вне модерации: ни мьютов, ни банов, ни спам-баллов.
    # Основную защиту даёт WhitelistMiddleware (TrustReason.FLIBUSTA); эта
    # строка — вторая линия на случай прямого вызова хендлера в обход роутера.
    if user_id and user_id == settings.flibusta_bot_id:
        return
    # Онлайн-запрос к блок-листу делаем только на первом сообщении автора:
    # снимок CAS проверяется всегда, он локальный и бесплатный.
    blocklist = await reputation.check(user_id, online=message_count == 0)
    # Один и тот же текст с нескольких аккаунтов — признак рассылки, который
    # виден даже когда в сообщении нет ни ссылок, ни стоп-слов.
    duplicate_authors = await duplicates.register(
        message.chat.id, user_id, message.text or message.caption or ""
    )
    context = build_context(
        message, settings, activity, message_count, blocklist, duplicate_authors
    )
    # Спам-сеть меняет текст, но не бота: считаем, сколько раз автор уже
    # рекламировал того же бота. Правка сообщения повтором не считается.
    promo_targets = engine.promo_targets(context)
    if promo is not None and promo_targets and user_id and not message.edit_date:
        repeats = await promo.register(message.chat.id, user_id, promo_targets)
        context = dataclasses.replace(context, promo_repeats=repeats)
    verdict = engine.evaluate(context)

    log_extra = {
        "component": "moderation",
        "chat_id": message.chat.id,
        "user_id": user_id,
        "username": user.username if user else None,
        "message_id": message.message_id,
        "message_count": message_count,
        "blocklist": blocklist.source or None,
        "duplicate_authors": duplicate_authors,
        "promo_targets": list(promo_targets),
        "promo_repeats": context.promo_repeats,
        **verdict.as_log_extra(),
    }

    if not verdict.is_spam:
        # Чистое сообщение приближает участника к статусу «одобренного».
        # Пропущенная реклама — нет: иначе непойманный спамер за семь сообщений
        # зарабатывает иммунитет и больше не проверяется вовсе.
        if user_id and not message.edit_date and not verdict.has_promo:
            await activity.register_clean_message(message.chat.id, user_id)
        logger.debug("Сообщение чистое", extra=log_extra)
        return

    logger.warning("Обнаружен спам: %s", verdict.explain(), extra=log_extra)
    if user_id:
        # Прогресс к одобрению сгорает — спамер начинает с нуля.
        await activity.reset(message.chat.id, user_id)

    await apply_punishment(
        message=message,
        bot=bot,
        settings=settings,
        verdict=verdict,
        events=events,
        deleted_registry=deleted_registry,
        log_extra=log_extra,
    )


async def apply_punishment(
    *,
    message: Message,
    bot: Bot,
    settings: Settings,
    verdict: Verdict,
    events: EventService,
    deleted_registry: DeletedMessageRegistry,
    log_extra: dict[str, Any],
) -> None:
    """Применяет санкцию, пишет событие в журнал и зовёт админов на разбор."""
    user = message.from_user
    user_id = user.id if user else 0
    deleted = False
    punished = False

    if settings.dry_run:
        logger.info("DRY-RUN: санкция не применялась", extra=log_extra)
    else:
        deleted = await safe_delete_message(message)
        if deleted:
            # Запоминаем удалённое: книжный бот может ответить на этот запрос
            # уже после удаления, и его ответ нужно будет убрать следом.
            deleted_registry.remember(message.chat.id, message.message_id)
        if verdict.action is Action.DELETE_AND_MUTE and user_id:
            punished = await safe_restrict_member(
                bot, message.chat.id, user_id, settings.mute_duration
            )
        elif verdict.action is Action.DELETE_AND_BAN:
            if user_id:
                punished = await safe_ban_member(bot, message.chat.id, user_id)
            elif message.sender_chat is not None:
                # Спам от имени постороннего канала: пользователя нет, банить
                # нужно сам канал.
                punished = await safe_ban_sender_chat(
                    bot, message.chat.id, message.sender_chat.id
                )

    event = await events.record(
        chat_id=message.chat.id,
        user_id=user_id,
        username=user.username if user else None,
        message_id=message.message_id,
        action=verdict.action.value,
        reason=verdict.reason.value,
        score=verdict.score,
        categories=",".join(sorted(c.value for c in verdict.categories)),
        signals=",".join(signal.kind.value for signal in verdict.signals),
        excerpt=message.text or message.caption or "",
        deleted=deleted,
    )

    # Карточка в админ-чат — единственный способ увидеть ложное срабатывание.
    if event is not None:
        await send_review_card(bot, settings, event, message)

    if settings.notify_chat and not settings.dry_run:
        await _notify_chat(bot, message, verdict, settings)

    logger.info(
        "Санкция применена",
        extra={
            **log_extra,
            "deleted": deleted,
            "punished": punished,
            "event_id": event.id if event else None,
        },
    )


async def _notify_chat(
    bot: Bot, message: Message, verdict: Verdict, settings: Settings
) -> None:
    """Короткое уведомление в чат; самоудаляется через `notify_ttl` секунд."""
    user = message.from_user
    mention = user.full_name if user else "Участник"
    label = _ACTION_LABELS.get(verdict.action, "сообщение удалено")
    categories = ", ".join(sorted(c.value for c in verdict.categories)) or "spam"
    text = (
        f"🛡 {hbold('Антиспам')}: {label}.\n"
        f"Отправитель: {mention}\n"
        f"Категория: {categories}"
    )
    try:
        notice = await bot.send_message(message.chat.id, text)
    except TelegramAPIError as exc:
        logger.info("Не удалось отправить уведомление: %s", exc)
        return

    if settings.notify_ttl > 0:
        task = asyncio.create_task(_delete_later(notice, settings.notify_ttl))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _delete_later(notice: Message, delay: int) -> None:
    """Удаляет служебное сообщение, чтобы не засорять чат."""
    await asyncio.sleep(delay)
    await safe_delete_message(notice)


def build_router(middleware: BaseMiddleware | None = None) -> Router:
    """Создаёт роутер модерации.

    Каждый вызов возвращает свежий объект: роутер-синглтон нельзя подключить
    к двум диспетчерам, а middleware на нём накапливались бы при повторной
    сборке — сообщение обрабатывалось бы дважды.
    """
    router = Router(name="moderation")
    groups = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
    router.message.filter(groups)
    router.edited_message.filter(groups)
    if middleware is not None:
        router.message.middleware(middleware)
        router.edited_message.middleware(middleware)
    router.message.register(moderate_message, F.text | F.caption)
    router.edited_message.register(moderate_message, F.text | F.caption)
    return router
