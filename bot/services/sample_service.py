"""Размеченные примеры сообщений: материал для правки правил."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from bot.db.models import MessageSample
from bot.db.session import Database

logger = logging.getLogger(__name__)

__all__ = ["SampleService", "SPAM", "HAM"]

SPAM = "spam"
HAM = "ham"


class SampleService:
    """Хранилище размеченных админами текстов."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @staticmethod
    def fingerprint(text: str) -> str:
        """sha256 нормализованного текста — ключ дедупликации."""
        return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()

    async def add(
        self,
        *,
        label: str,
        chat_id: int,
        text: str,
        author_id: int = 0,
        added_by: int = 0,
    ) -> MessageSample | None:
        """Добавляет пример. Возвращает `None`, если такой текст уже размечен."""
        clean = text.strip()
        if not clean:
            return None
        sample = MessageSample(
            chat_id=chat_id,
            label=label,
            text=clean[:4000],
            text_hash=self.fingerprint(clean),
            author_id=author_id,
            added_by=added_by,
        )
        try:
            async with self._db.session() as session:
                session.add(sample)
                await session.commit()
                await session.refresh(sample)
        except IntegrityError:
            return None
        except SQLAlchemyError as exc:
            logger.error("Не удалось сохранить пример: %s", exc, exc_info=True)
            return None
        return sample

    async def counts(self) -> dict[str, int]:
        """Сколько примеров каждого класса собрано."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(MessageSample.label, func.count(MessageSample.id)).group_by(
                        MessageSample.label
                    )
                )
                return {label: int(count) for label, count in result.all()}
        except SQLAlchemyError as exc:
            logger.error("Не удалось посчитать примеры: %s", exc, exc_info=True)
            return {}

    async def all_samples(self, limit: int = 2000) -> list[MessageSample]:
        """Вся выборка целиком — материал для регрессионного прогона."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(MessageSample).order_by(MessageSample.id).limit(limit)
                )
                return list(result.scalars().all())
        except SQLAlchemyError as exc:
            logger.error("Не удалось прочитать выборку: %s", exc, exc_info=True)
            return []

    async def recent(self, label: str, limit: int = 20) -> list[MessageSample]:
        """Последние примеры класса — для ручного прогона движком."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(MessageSample)
                    .where(MessageSample.label == label)
                    .order_by(MessageSample.id.desc())
                    .limit(limit)
                )
                return list(result.scalars().all())
        except SQLAlchemyError as exc:
            logger.error("Не удалось прочитать примеры: %s", exc, exc_info=True)
            return []
