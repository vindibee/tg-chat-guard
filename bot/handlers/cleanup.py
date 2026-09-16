"""Массовая очистка чата: `/clean` и `/purge`.

Команда разрушительная и необратимая, поэтому вокруг неё три предохранителя:

1. права проверяются **заново** через `get_chat_member` — кэш админов живёт
   пять минут, а для удаления сотен сообщений этого мало;
2. глубина ограничена потолком из настроек: опечатка `/clean 999999` не
   выносит всю историю;
3. на большой глубине бот спрашивает подтверждение кнопкой.

Что бот физически не может: Telegram разрешает ботам удалять только сообщения
не старше 48 часов и не даёт списка истории — id перебираются назад от команды.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.filters.admin import IsChatAdmin
from bot.services.purge import PurgeStats, purge_recent
from bot.utils.telegram import safe_delete_message

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

logger = logging.getLogger(__name__)

#: Статусы, дающие право на массовое удаление.
ADMIN_STATUSES: Final[frozenset[str]] = frozenset({"administrator", "creator"})

#: Фоновые задачи самоудаления отчётов.
_TASKS: Final[set[asyncio.Task[Any]]] = set()

__all__ = ["clean_command", "confirm_purge", "build_router", "PurgeCallback", "parse_depth"]


class PurgeCallback(CallbackData, prefix="purge"):
    """Данные кнопок подтверждения."""

    action: str  # go | no
    depth: int
    anchor: int
    user: int


def parse_depth(raw: str | None, settings: Settings) -> int:
    """Разбирает аргумент команды в глубину очистки.

    Пусто или `all` — глубина по умолчанию. Число ограничивается потолком.

    Raises:
        ValueError: аргумент не число и не `all`.
    """
    argument = (raw or "").strip().lower()
    if not argument or argument in {"all", "все", "всё"}:
        return min(settings.clean_default_depth, settings.clean_max_depth)
    if not argument.isdigit():
        raise ValueError(argument)
    return max(1, min(int(argument), settings.clean_max_depth))


async def can_purge(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Свежая проверка прав именно этого пользователя.

    Владелец чата может всё. Администратору нужно право удалять сообщения:
    без него команда всё равно не сработает, и честнее сказать об этом сразу.
    """
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.warning("Не удалось проверить права: %s", exc, extra={"component": "purge"})
        return False
    if member.status not in ADMIN_STATUSES:
        return False
    if member.status == "creator":
        return True
    return bool(getattr(member, "can_delete_messages", False))


def confirmation_keyboard(depth: int, anchor: int, user_id: int) -> InlineKeyboardMarkup:
    """Кнопки подтверждения большой уборки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🧹 Да, удалить {depth}",
                    callback_data=PurgeCallback(
                        action="go", depth=depth, anchor=anchor, user=user_id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=PurgeCallback(
                        action="no", depth=depth, anchor=anchor, user=user_id
                    ).pack(),
                ),
            ]
        ]
    )


async def run_purge(
    bot: Bot,
    settings: Settings,
    chat_id: int,
    anchor_id: int,
    depth: int,
    *,
    extra_exclude: tuple[int, ...] = (),
) -> PurgeStats:
    """Выполняет уборку и присылает самоудаляющийся отчёт."""
    notice: Message | None = None
    try:
        notice = await bot.send_message(chat_id, f"🧹 Убираю последние {depth} сообщений…")
    except TelegramAPIError as exc:
        logger.info("Не удалось отправить отчёт об уборке: %s", exc)

    exclude = (anchor_id, *extra_exclude) + ((notice.message_id,) if notice else ())
    stats = await purge_recent(
        bot,
        chat_id=chat_id,
        anchor_message_id=anchor_id,
        depth=depth,
        pause=settings.clean_batch_pause,
        exclude=exclude,
    )

    text = f"🧹 Чат очищен — {stats.summary}."
    if notice is not None:
        with contextlib.suppress(TelegramAPIError):
            await notice.edit_text(text)
    else:
        with contextlib.suppress(TelegramAPIError):
            notice = await bot.send_message(chat_id, text)

    # Отчёт и сама команда живут несколько секунд и исчезают, чтобы уборка
    # не оставляла после себя новый мусор.
    task = asyncio.create_task(
        _cleanup_traces(bot, chat_id, notice, anchor_id, settings.clean_notice_ttl)
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return stats


async def _cleanup_traces(
    bot: Bot, chat_id: int, notice: Message | None, anchor_id: int, delay: int
) -> None:
    """Убирает отчёт и команду через `delay` секунд."""
    if delay > 0:
        await asyncio.sleep(delay)
    if notice is not None:
        await safe_delete_message(notice)
    with contextlib.suppress(TelegramAPIError):
        await bot.delete_message(chat_id=chat_id, message_id=anchor_id)


async def clean_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    settings: Settings,
    **_: Any,
) -> None:
    """`/clean [N|all]` — удаляет последние сообщения чата."""
    user = message.from_user
    if user is None:
        return
    if not settings.clean_enabled:
        await message.reply("Массовая очистка отключена в настройках бота.")
        return

    try:
        depth = parse_depth(command.args, settings)
    except ValueError:
        await message.reply(
            "Использование: <code>/clean 300</code> или <code>/clean all</code>.\n"
            f"Максимум за раз — {settings.clean_max_depth}."
        )
        return

    # Права проверяем заново: кэш админов для разрушительной команды не годится.
    if not await can_purge(bot, message.chat.id, user.id):
        await message.reply(
            "Команда только для администраторов с правом «Удаление сообщений»."
        )
        return

    if depth >= settings.clean_confirm_threshold:
        await message.reply(
            f"Удалить последние <b>{depth}</b> сообщений? Это необратимо.\n"
            "<i>Telegram позволяет ботам удалять сообщения не старше 48 часов — "
            "более старое останется на месте.</i>",
            reply_markup=confirmation_keyboard(depth, message.message_id, user.id),
        )
        return

    logger.info(
        "Запущена массовая уборка",
        extra={
            "component": "purge",
            "chat_id": message.chat.id,
            "admin_id": user.id,
            "depth": depth,
        },
    )
    await run_purge(bot, settings, message.chat.id, message.message_id, depth)


async def confirm_purge(
    callback: CallbackQuery,
    callback_data: PurgeCallback,
    bot: Bot,
    settings: Settings,
    **_: Any,
) -> None:
    """Обрабатывает кнопки подтверждения большой уборки."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Карточка устарела.", show_alert=True)
        return

    if callback.from_user.id != callback_data.user:
        await callback.answer("Подтвердить может только тот, кто запустил.", show_alert=True)
        return

    if callback_data.action == "no":
        with contextlib.suppress(TelegramAPIError):
            await message.edit_text("Уборка отменена.")
        await callback.answer("Отменено.")
        await safe_delete_message(message)
        return

    # Права могли измениться, пока висело подтверждение.
    if not await can_purge(bot, message.chat.id, callback.from_user.id):
        await callback.answer("Права на удаление сообщений больше нет.", show_alert=True)
        return

    await callback.answer("Убираю…")
    with contextlib.suppress(TelegramAPIError):
        await message.delete()

    logger.info(
        "Массовая уборка подтверждена",
        extra={
            "component": "purge",
            "chat_id": message.chat.id,
            "admin_id": callback.from_user.id,
            "depth": callback_data.depth,
        },
    )
    await run_purge(
        bot,
        settings,
        message.chat.id,
        callback_data.anchor,
        callback_data.depth,
        extra_exclude=(message.message_id,),
    )


def build_router() -> Router:
    """Роутер массовой очистки: только группы, только админы."""
    router = Router(name="cleanup")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
    router.message.register(clean_command, Command("clean", "purge"), IsChatAdmin())
    router.callback_query.register(confirm_purge, PurgeCallback.filter())
    return router
