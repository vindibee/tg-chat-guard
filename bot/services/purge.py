"""Массовое удаление сообщений пакетами.

Ограничения Telegram, из которых растёт вся логика:

* `deleteMessages` принимает не больше **100** id за вызов;
* удалить можно только сообщение **не старше 48 часов**;
* боту нужно право `can_delete_messages`;
* API не даёт списка сообщений чата, поэтому id перебираются **назад**
  от текущего — других способов «почистить последние N» у ботов нет.

Отсюда же честность отчёта: Telegram не сообщает, сколько сообщений из пакета
реально существовало, поэтому считаем принятые id и говорим «удалено до N».
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

if TYPE_CHECKING:  # pragma: no cover
    from aiogram import Bot

logger = logging.getLogger(__name__)

__all__ = ["PurgeStats", "purge_recent", "build_id_batches", "BATCH_LIMIT"]

#: Предел Telegram на один вызов deleteMessages.
BATCH_LIMIT: int = 100

#: Сколько подряд неудачных пакетов считать «упёрлись в 48 часов».
EMPTY_STREAK_LIMIT: int = 3


@dataclass(frozen=True, slots=True)
class PurgeStats:
    """Итог уборки."""

    requested: int
    attempted: int
    skipped: int
    batches: int
    failed_batches: int
    stopped_early: bool = False
    forbidden: bool = False

    @property
    def summary(self) -> str:
        """Человекочитаемый итог для чата."""
        if self.forbidden:
            return "не хватает права «Удаление сообщений»"
        parts = [f"удалено до {self.attempted}"]
        if self.skipped:
            parts.append(f"{self.skipped} пропущено (старше 48 часов или уже удалены)")
        if self.stopped_early:
            parts.append("дальше начинаются сообщения старше 48 часов")
        return ", ".join(parts)


def build_id_batches(
    anchor_message_id: int,
    depth: int,
    *,
    exclude: Iterable[int] = (),
    batch_size: int = BATCH_LIMIT,
) -> list[list[int]]:
    """Готовит пакеты id, идя назад от якорного сообщения.

    Args:
        anchor_message_id: сообщение, от которого считаем назад (сама команда).
        depth: сколько id просмотреть.
        exclude: id, которые трогать нельзя (например, сама команда и отчёт).
        batch_size: размер пакета, не больше `BATCH_LIMIT`.
    """
    size = max(1, min(batch_size, BATCH_LIMIT))
    skip = set(exclude)
    ids = [
        message_id
        for message_id in range(anchor_message_id - 1, max(anchor_message_id - depth - 1, 0), -1)
        if message_id > 0 and message_id not in skip
    ]
    return [ids[start : start + size] for start in range(0, len(ids), size)]


async def purge_recent(
    bot: Bot,
    chat_id: int,
    anchor_message_id: int,
    depth: int,
    *,
    pause: float = 0.3,
    exclude: Sequence[int] = (),
) -> PurgeStats:
    """Удаляет до `depth` последних сообщений чата пакетами по 100.

    Неудачный пакет не прерывает уборку: сообщения могли быть удалены раньше
    или оказаться старше 48 часов. Но если подряд падает несколько пакетов,
    дальше идти бессмысленно — там заведомо старая история.
    """
    batches = build_id_batches(anchor_message_id, depth, exclude=exclude)
    attempted = skipped = failed = 0
    empty_streak = 0

    for index, batch in enumerate(batches):
        try:
            await _delete_batch(bot, chat_id, batch)
        except TelegramForbiddenError as exc:
            logger.warning(
                "Нет прав на массовое удаление: %s",
                exc.message,
                extra={"component": "purge", "chat_id": chat_id},
            )
            return PurgeStats(
                requested=depth,
                attempted=attempted,
                skipped=skipped + sum(len(rest) for rest in batches[index:]),
                batches=index,
                failed_batches=failed,
                forbidden=True,
            )
        except TelegramBadRequest as exc:
            # Типовая причина: часть id уже удалена или старше 48 часов.
            failed += 1
            skipped += len(batch)
            empty_streak += 1
            logger.debug(
                "Пакет не удалён: %s",
                exc.message,
                extra={"component": "purge", "chat_id": chat_id, "batch": index},
            )
            if empty_streak >= EMPTY_STREAK_LIMIT:
                logger.info(
                    "Уборка остановлена: дальше идёт история старше 48 часов",
                    extra={"component": "purge", "chat_id": chat_id, "batches": index + 1},
                )
                return PurgeStats(
                    requested=depth,
                    attempted=attempted,
                    skipped=skipped + sum(len(rest) for rest in batches[index + 1 :]),
                    batches=index + 1,
                    failed_batches=failed,
                    stopped_early=True,
                )
        else:
            attempted += len(batch)
            empty_streak = 0

        if pause and index + 1 < len(batches):
            await asyncio.sleep(pause)

    stats = PurgeStats(
        requested=depth,
        attempted=attempted,
        skipped=skipped,
        batches=len(batches),
        failed_batches=failed,
    )
    logger.info(
        "Массовая уборка завершена",
        extra={
            "component": "purge",
            "chat_id": chat_id,
            "requested": stats.requested,
            "attempted": stats.attempted,
            "skipped": stats.skipped,
            "failed_batches": stats.failed_batches,
        },
    )
    return stats


async def _delete_batch(bot: Bot, chat_id: int, batch: list[int]) -> None:
    """Один вызов deleteMessages с одной повторной попыткой при FloodWait."""
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=batch)
    except TelegramRetryAfter as exc:
        logger.info(
            "FloodWait при уборке, ждём %s с", exc.retry_after, extra={"component": "purge"}
        )
        await asyncio.sleep(exc.retry_after)
        await bot.delete_messages(chat_id=chat_id, message_ids=batch)
