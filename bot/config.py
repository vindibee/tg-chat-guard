"""Загрузка и валидация настроек приложения (pydantic-settings v2)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Final

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Домены, ссылки на которые не считаются спам-фактором «из коробки».
DEFAULT_SAFE_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "flibusta.is",
        "flibusta.site",
        "flibusta.net",
        "fantlab.ru",
        "author.today",
        "litres.ru",
        "goodreads.com",
        "libgen.is",
        "libgen.rs",
        "wikipedia.org",
        "habr.com",
        "github.com",
    }
)


def _split_csv(value: Any) -> Any:
    """Превращает `"1, 2 ,3"` в `["1", "2", "3"]`, оставляя списки как есть."""
    if isinstance(value, str):
        return [chunk.strip() for chunk in value.replace(";", ",").split(",") if chunk.strip()]
    return value


class Settings(BaseSettings):
    """Единая точка конфигурации. Все значения читаются из окружения/.env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram -----------------------------------------------------------
    bot_token: str = Field(..., min_length=20, description="Токен бота из @BotFather")
    parse_mode: str = "HTML"
    drop_pending_updates: bool = True

    # --- Инфраструктура -----------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./moderation.db",
        description="Async DSN SQLAlchemy 2.0 (asyncpg/aiosqlite).",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_namespace: str = "tgmod"
    whitelist_cache_ttl: int = Field(default=900, ge=30, description="TTL кэша белого списка, сек.")
    db_echo: bool = False
    #: Накатывать миграции при старте. Выключите, если схему обновляет деплой
    #: отдельным шагом (`alembic upgrade head`).
    run_migrations_on_startup: bool = True

    # --- Логирование --------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    # --- Доверенные сущности ------------------------------------------------
    flibusta_bot_id: int | None = Field(
        default=None, description="user_id бота Флибусты — иммунитет к любым проверкам."
    )
    flibusta_username: str = Field(default="flibustafreebookbot")
    #: Дополнительные системные боты/пользователи из .env (статический уровень whitelist).
    static_whitelist_ids: Annotated[set[int], NoDecode] = Field(default_factory=set)
    static_whitelist_usernames: Annotated[set[str], NoDecode] = Field(default_factory=set)
    #: Суперадмины бота — имеют доступ к /whitelist_* в любом чате.
    super_admin_ids: Annotated[set[int], NoDecode] = Field(default_factory=set)

    #: Приватный чат модерации: туда уходит карточка по каждому срабатыванию
    #: с кнопками отката. Без него ложные срабатывания остаются невидимыми.
    admin_log_chat_id: int | None = None
    #: Сколько чистых сообщений подряд делают участника «одобренным».
    #: Одобренные не проверяются вовсе — это главный приём против ложных
    #: срабатываний у зрелых антиспам-ботов (tg-spam: --first-messages-count).
    approved_after_messages: int = Field(default=7, ge=1)
    #: Пока сообщений меньше — участник считается новичком (+вес в скоре).
    new_member_messages: int = Field(default=5, ge=0)
    #: Проверять всех, включая одобренных. Включать только во время атаки.
    paranoid_mode: bool = False
    #: Возвращать текст в чат при откате ложного срабатывания.
    restore_on_false_positive: bool = True

    # --- Внешние блок-листы -------------------------------------------------
    #: Проверять авторов по публичным реестрам спамеров.
    reputation_enabled: bool = True
    #: Источники: `lols` (онлайн-запрос) и `cas_export` (снимок списка в памяти).
    reputation_providers: Annotated[set[str], NoDecode] = Field(
        default_factory=lambda: {"lols", "cas_export"}
    )
    #: Вес попадания в блок-лист. Это сигнал, а не приговор: базы ошибаются.
    reputation_weight: float = Field(default=5.0, ge=0)
    #: Банить сразу при попадании в блок-лист (по умолчанию — только сигнал).
    reputation_autoban: bool = False
    #: Таймаут онлайн-запроса к блок-листу, сек.
    reputation_timeout: float = Field(default=2.0, gt=0)
    #: Как часто обновлять снимок экспорта CAS, сек. (по умолчанию 12 ч).
    cas_sync_interval: int = Field(default=43200, ge=600)
    #: Сколько хранить ответ блок-листа в Redis, сек.
    reputation_cache_ttl: int = Field(default=86400, ge=60)

    # --- Массовая очистка (/clean) ------------------------------------------
    #: Разрешить админам массовое удаление сообщений.
    clean_enabled: bool = True
    #: Сколько сообщений чистит `/clean all`.
    clean_default_depth: int = Field(default=1000, ge=1)
    #: Жёсткий потолок глубины: защита от опечатки вида `/clean 999999`.
    clean_max_depth: int = Field(default=5000, ge=1)
    #: Начиная с этой глубины бот спросит подтверждение кнопкой.
    clean_confirm_threshold: int = Field(default=200, ge=1)
    #: Пауза между пакетами удаления, сек. — страховка от FloodWait.
    clean_batch_pause: float = Field(default=0.3, ge=0)
    #: Через сколько секунд убрать отчёт об очистке.
    clean_notice_ttl: int = Field(default=5, ge=0)

    # --- Ответы бота Флибусты ------------------------------------------------
    #: Удалять «мусорные» ответы книжного бота: «не найдено книг» и ответы
    #: на сообщения, которые антиспам уже удалил.
    flibusta_cleanup: bool = True

    # --- Служебные сообщения ------------------------------------------------
    #: Какие группы служебных сообщений удалять: `join`, `leave`, `pin`, `title`,
    #: `photo`, `videochat`, `forum`, `boost`, `giveaway`, `gift`, `created`,
    #: `other`. Особые значения: `all` и `none`. Платежи не удаляются никогда.
    service_cleanup: Annotated[set[str], NoDecode] = Field(
        default_factory=lambda: {
            "join",
            "leave",
            "title",
            "photo",
            "videochat",
            "boost",
            "created",
            "forum",
            "giveaway",
            "gift",
            "other",
        }
    )

    # --- Дубликаты ----------------------------------------------------------
    #: Искать одинаковые сообщения от разных участников.
    duplicate_enabled: bool = True
    #: Сколько РАЗНЫХ авторов одного текста считаются рассылкой.
    duplicate_threshold: int = Field(default=3, ge=2)
    #: Окно наблюдения, сек.
    duplicate_window: int = Field(default=3600, ge=60)
    #: Короткие сообщения не проверяем: «спасибо» и «ап» совпадают у всех.
    duplicate_min_length: int = Field(default=40, ge=10)
    #: Базовый вес совпадения; растёт с числом авторов.
    duplicate_weight: float = Field(default=4.0, ge=0)

    # --- Жалобы участников --------------------------------------------------
    #: Команда /report доступна обычным участникам.
    report_enabled: bool = True
    #: Сколько жалоб на сообщение зовут администраторов.
    report_threshold: int = Field(default=2, ge=1)

    #: Доверять администраторам чата (не модерировать их сообщения).
    trust_chat_admins: bool = True
    #: Доверять анонимным админам и сообщениям от имени канала-владельца.
    trust_anonymous_admins: bool = True

    # --- Антиспам: пороги и веса -------------------------------------------
    safe_domains: Annotated[set[str], NoDecode] = Field(
        default_factory=lambda: set(DEFAULT_SAFE_DOMAINS)
    )
    #: Наказывать только при наличии хотя бы одного «жёсткого» спам-фактора.
    require_spam_factor: bool = True
    #: Порог удаления сообщения.
    delete_threshold: float = Field(default=6.0, gt=0)
    #: Порог «удалить + выдать mute».
    mute_threshold: float = Field(default=9.0, gt=0)
    #: Порог «удалить + забанить».
    ban_threshold: float = Field(default=13.0, gt=0)
    #: Длительность mute в секундах.
    mute_duration: int = Field(default=3600, ge=30)
    #: Сообщать ли в чат о принятых мерах.
    notify_chat: bool = True
    #: Через сколько секунд удалять служебное уведомление (0 — не удалять).
    notify_ttl: int = Field(default=20, ge=0)
    #: Не удалять сообщение, только логировать (shadow-режим для обкатки правил).
    dry_run: bool = False

    @field_validator("flibusta_bot_id", "admin_log_chat_id", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        """Пустая строка в .env (`ADMIN_LOG_CHAT_ID=`) означает «не задано»."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "static_whitelist_ids",
        "super_admin_ids",
        "static_whitelist_usernames",
        "safe_domains",
        "reputation_providers",
        "service_cleanup",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator(
        "static_whitelist_usernames",
        "safe_domains",
        "reputation_providers",
        "service_cleanup",
        mode="after",
    )
    @classmethod
    def _normalize_lower(cls, value: set[str]) -> set[str]:
        return {item.lstrip("@").strip().lower() for item in value if item.strip()}

    @field_validator("flibusta_username", mode="after")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        return value.lstrip("@").strip().lower()

    @field_validator("log_level", mode="after")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_thresholds(self) -> Settings:
        if self.clean_confirm_threshold > self.clean_max_depth:
            raise ValueError(
                "clean_confirm_threshold не может превышать clean_max_depth: "
                "иначе подтверждение никогда не сработает"
            )
        if not (self.delete_threshold <= self.mute_threshold <= self.ban_threshold):
            raise ValueError(
                "Пороги должны возрастать: delete_threshold <= mute_threshold <= ban_threshold"
            )
        return self

    # --- Производные значения ----------------------------------------------
    @property
    def protected_ids(self) -> frozenset[int]:
        """Статический whitelist по id (включая Флибусту и суперадминов)."""
        ids: set[int] = set(self.static_whitelist_ids) | set(self.super_admin_ids)
        if self.flibusta_bot_id is not None:
            ids.add(self.flibusta_bot_id)
        return frozenset(ids)

    @property
    def protected_usernames(self) -> frozenset[str]:
        """Статический whitelist по username в нижнем регистре, без `@`."""
        names = set(self.static_whitelist_usernames)
        if self.flibusta_username:
            names.add(self.flibusta_username)
        return frozenset(names)

    def redis_key(self, *parts: str | int) -> str:
        """Строит ключ Redis в пространстве имён приложения."""
        return ":".join((self.redis_namespace, *(str(part) for part in parts)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек."""
    return Settings()  # type: ignore[call-arg]
