"""Журнал модерации: запись событий и их разбор администратором."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bot.db.models import ModerationEvent
from bot.db.session import Database

logger = logging.getLogger(__name__)

__all__ = ["EventService"]


class EventService:
    """Хранилище событий модерации.

    Сбой БД никогда не влияет на саму модерацию: методы возвращают `None`
    и пишут ошибку в лог.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def record(
        self,
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        message_id: int,
        action: str,
        reason: str,
        score: float,
        categories: str,
        signals: str,
        excerpt: str,
        deleted: bool,
    ) -> ModerationEvent | None:
        """Пишет событие и возвращает его вместе с присвоенным id."""
        event = ModerationEvent(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            message_id=message_id,
            action=action,
            reason=reason,
            score=score,
            categories=categories[:255],
            signals=signals[:512],
            excerpt=excerpt[:1000],
            deleted=deleted,
        )
        try:
            async with self._db.session() as session:
                session.add(event)
                await session.commit()
                await session.refresh(event)
        except SQLAlchemyError as exc:
            logger.error("Не удалось записать событие модерации: %s", exc, exc_info=True)
            return None
        return event

    async def get(self, event_id: int) -> ModerationEvent | None:
        """Читает событие по id."""
        try:
            async with self._db.session() as session:
                return await session.get(ModerationEvent, event_id)
        except SQLAlchemyError as exc:
            logger.error("Не удалось прочитать событие %s: %s", event_id, exc, exc_info=True)
            return None

    async def find_by_message(self, chat_id: int, message_id: int) -> ModerationEvent | None:
        """Последнее событие по конкретному сообщению (для `/ham`)."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(ModerationEvent)
                    .where(
                        ModerationEvent.chat_id == chat_id,
                        ModerationEvent.message_id == message_id,
                    )
                    .order_by(ModerationEvent.id.desc())
                    .limit(1)
                )
                return result.scalars().first()
        except SQLAlchemyError as exc:
            logger.error("Не удалось найти событие по сообщению: %s", exc, exc_info=True)
            return None

    async def mark_reviewed(
        self, event_id: int, *, admin_id: int, false_positive: bool
    ) -> ModerationEvent | None:
        """Отмечает событие разобранным.

        Args:
            admin_id: кто нажал кнопку.
            false_positive: `True` — срабатывание признано ложным.

        Returns:
            Обновлённое событие либо `None`, если его нет или БД недоступна.
        """
        try:
            async with self._db.session() as session:
                event = await session.get(ModerationEvent, event_id)
                if event is None:
                    return None
                event.false_positive = false_positive
                event.reviewed_by = admin_id
                event.reviewed_at = datetime.now(tz=UTC)
                await session.commit()
                await session.refresh(event)
                return event
        except SQLAlchemyError as exc:
            logger.error("Не удалось обновить событие %s: %s", event_id, exc, exc_info=True)
            return None

    async def recent_false_positives(
        self, chat_id: int, limit: int = 20
    ) -> list[ModerationEvent]:
        """Последние подтверждённые ложные срабатывания — материал для правил."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(ModerationEvent)
                    .where(
                        ModerationEvent.chat_id == chat_id,
                        ModerationEvent.false_positive.is_(True),
                    )
                    .order_by(ModerationEvent.id.desc())
                    .limit(limit)
                )
                return list(result.scalars().all())
        except SQLAlchemyError as exc:
            logger.error("Не удалось прочитать ложные срабатывания: %s", exc, exc_info=True)
            return []
