"""Точка входа: сборка зависимостей, регистрация роутеров, запуск polling."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import Settings, get_settings
from bot.db.migrator import run_migrations
from bot.db.session import Database, create_database
from bot.handlers import (
    admin_review,
    admin_training,
    admin_whitelist,
    join_guard,
    moderation,
    service_cleanup,
)
from bot.middlewares.dependencies import DependenciesMiddleware
from bot.middlewares.whitelist_middleware import WhitelistMiddleware
from bot.services.activity_tracker import ActivityTracker
from bot.services.admin_cache import ChatAdminCache
from bot.services.antispam_engine import AntiSpamEngine, EngineConfig
from bot.services.duplicate_detector import DuplicateDetector
from bot.services.event_service import EventService
from bot.services.report_service import ReportService
from bot.services.reputation_service import ReputationService
from bot.services.sample_service import SampleService
from bot.services.whitelist_service import WhitelistService
from bot.utils.logging import setup_logging

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


async def create_redis(settings: Settings) -> Redis | None:
    """Подключается к Redis. При недоступности возвращает `None`.

    Бот остаётся работоспособным без Redis: белый список читается из БД,
    теряется только кэш и счётчики активности.
    """
    try:
        from redis.asyncio import Redis
    except ImportError:  # pragma: no cover
        logger.warning("Пакет redis не установлен — кэш отключён")
        return None

    client: Redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=2.0
    )
    try:
        await client.ping()
    except Exception as exc:
        logger.warning(
            "Redis недоступен (%s) — работаем без кэша", exc, extra={"stage": "startup"}
        )
        await client.aclose()
        return None
    logger.info("Redis подключён", extra={"stage": "startup"})
    return client


def build_dispatcher(
    settings: Settings,
    database: Database,
    redis: Redis | None,
    reputation: ReputationService | None = None,
) -> Dispatcher:
    """Собирает диспетчер со всеми зависимостями и роутерами."""
    whitelist = WhitelistService(settings, database, redis)
    admin_cache = ChatAdminCache()
    activity = ActivityTracker(settings, redis)
    events = EventService(database)
    samples = SampleService(database)
    reports = ReportService(settings, redis)
    duplicates = DuplicateDetector(settings, redis)
    reputation = reputation or ReputationService(settings, redis)
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.outer_middleware(
        DependenciesMiddleware(
            settings=settings,
            database=database,
            whitelist=whitelist,
            admin_cache=admin_cache,
            activity=activity,
            engine=engine,
            events=events,
            samples=samples,
            reports=reports,
            reputation=reputation,
            duplicates=duplicates,
        )
    )

    # Порядок важен: админские команды и карточки разбора — до антиспама.
    dispatcher.include_router(admin_review.build_router())
    dispatcher.include_router(admin_training.build_router())
    dispatcher.include_router(admin_whitelist.build_router())
    dispatcher.include_router(join_guard.build_router())
    # Уборка служебных сообщений — до модерации: у них нет текста, и антиспаму
    # они всё равно не достанутся.
    dispatcher.include_router(service_cleanup.build_router(settings))

    trust_gate = WhitelistMiddleware(settings, whitelist, admin_cache, activity)
    dispatcher.include_router(moderation.build_router(trust_gate))

    dispatcher["whitelist_service"] = whitelist
    dispatcher["reputation_service"] = reputation
    return dispatcher


async def run() -> None:
    """Полный жизненный цикл приложения."""
    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.log_json)
    logger.info(
        "Запуск бота-модератора",
        extra={
            "stage": "startup",
            "dry_run": settings.dry_run,
            "flibusta_bot_id": settings.flibusta_bot_id,
            "static_whitelist": len(settings.protected_ids),
        },
    )

    database = create_database(settings.database_url, echo=settings.db_echo)
    if settings.run_migrations_on_startup:
        outcome = await run_migrations(database)
        logger.info(
            "Схема БД готова",
            extra={"stage": "startup", "migrations": outcome.value},
        )
    else:
        logger.info(
            "Миграции при старте отключены — ожидается `alembic upgrade head`",
            extra={"stage": "startup"},
        )
    redis = await create_redis(settings)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    reputation = ReputationService(settings, redis)
    dispatcher = build_dispatcher(settings, database, redis, reputation)

    try:
        me = await bot.get_me()
        logger.info(
            "Бот авторизован",
            extra={"stage": "startup", "bot_username": me.username, "bot_id": me.id},
        )
        # Снимок CAS качается в фоне, чтобы не задерживать старт бота.
        await reputation.start_background_sync()
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=settings.drop_pending_updates,
        )
    finally:
        logger.info("Остановка бота", extra={"stage": "shutdown"})
        await reputation.close()
        await bot.session.close()
        if redis is not None:
            await redis.aclose()
        await database.dispose()


def main() -> None:
    """Синхронная обёртка для запуска из консоли."""
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Остановлено пользователем")


if __name__ == "__main__":
    main()
