"""ORM-модели (SQLAlchemy 2.0, декларативный стиль с аннотациями)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "WhitelistEntry",
    "ModerationEvent",
    "MessageSample",
    "GLOBAL_SCOPE",
]

#: `chat_id = 0` означает глобальную запись (действует во всех чатах).
GLOBAL_SCOPE: int = 0


#: SQLite умеет автоинкремент только для INTEGER PRIMARY KEY, а Postgres
#: требует BIGINT под объёмы журнала модерации — отсюда вариант на диалект.
PrimaryKeyType = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Базовый класс моделей."""


class WhitelistEntry(Base):
    """Динамическая запись белого списка, управляемая админами чата.

    Note:
        Вместо `NULL` используются нейтральные значения (`user_id=0`, `username=""`),
        потому что в PostgreSQL `NULL` не участвует в уникальных ограничениях —
        иначе один и тот же пользователь мог бы попасть в список многократно.
    """

    __tablename__ = "whitelist_entries"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", "username", name="uq_whitelist_target"),
        Index("ix_whitelist_lookup", "chat_id", "user_id"),
        Index("ix_whitelist_username", "chat_id", "username"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyType, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=GLOBAL_SCOPE, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    username: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    @property
    def is_global(self) -> bool:
        return self.chat_id == GLOBAL_SCOPE

    def describe(self) -> str:
        """Короткое описание для админского вывода."""
        target = f"@{self.username}" if self.username else f"id={self.user_id}"
        scope = "глобально" if self.is_global else f"чат {self.chat_id}"
        suffix = f" — {self.reason}" if self.reason else ""
        return f"{target} ({scope}){suffix}"

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"WhitelistEntry(id={self.id}, chat_id={self.chat_id}, "
            f"user_id={self.user_id}, username={self.username!r})"
        )


class ModerationEvent(Base):
    """Журнал модерации: что, кому и почему прилетело."""

    __tablename__ = "moderation_events"
    __table_args__ = (Index("ix_events_chat_created", "chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(PrimaryKeyType, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    categories: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    signals: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # --- Разбор администратором ------------------------------------------
    #: Администратор нажал «Не спам» — срабатывание признано ложным.
    false_positive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Кто разобрал карточку (0 — ещё никто).
    reviewed_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_reviewed(self) -> bool:
        return bool(self.reviewed_by)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ModerationEvent(chat_id={self.chat_id}, user_id={self.user_id}, "
            f"action={self.action!r}, score={self.score})"
        )


class MessageSample(Base):
    """Размеченный админом пример: спам или «хороший» текст.

    Нужен для правки правил и как регрессионный материал: тексты, на которых
    фильтр однажды ошибся, прогоняются движком после каждого изменения словаря.
    """

    __tablename__ = "message_samples"
    __table_args__ = (
        UniqueConstraint("label", "text_hash", name="uq_sample_text"),
        Index("ix_samples_label", "label"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyType, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: `spam` или `ham`.
    label: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256 текста — защита от дублей в выборке.
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"MessageSample(id={self.id}, label={self.label!r}, chat_id={self.chat_id})"
