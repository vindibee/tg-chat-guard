"""Проверка авторов по публичным реестрам спамеров Telegram.

Два независимых источника:

* **LOLS** (`api.lols.bot/account?id=`) — онлайн-запрос, ответ кэшируется в Redis.
* **CAS** (`api.cas.chat/export.csv`) — снимок списка (~1.3 млн id, 14 МБ).
  Держим его отсортированным `array("q")` прямо в процессе: ~11 МБ памяти и
  поиск делением пополам. В Redis-множестве тот же список занял бы ~70 МБ.

Точечная ручка CAS (`/check`) намеренно не используется: на проверке она не
находит даже id из собственного экспорта.

Любая ошибка сети трактуется как «чисто»: блок-лист усиливает решение, но не
должен его блокировать.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
from array import array
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import aiohttp

from bot.utils.ttl_cache import TTLCache

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["ReputationService", "ReputationVerdict"]

LOLS_URL: Final[str] = "https://api.lols.bot/account"
CAS_EXPORT_URL: Final[str] = "https://api.cas.chat/export.csv"


@dataclass(frozen=True, slots=True)
class ReputationVerdict:
    """Ответ блок-листов по одному пользователю."""

    banned: bool = False
    source: str = ""
    when: str | None = None

    def __bool__(self) -> bool:
        return self.banned


class ReputationService:
    """Клиент публичных блок-листов с кэшем и фоновой синхронизацией CAS."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._session = session
        self._own_session = session is None
        #: Отсортированный снимок экспорта CAS.
        self._cas: array[int] = array("q")
        #: Кэш ответов LOLS в памяти процесса — работает и без Redis.
        self._local: TTLCache[int, ReputationVerdict] = TTLCache(
            ttl=float(settings.reputation_cache_ttl), maxsize=20_000
        )
        self._sync_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    #                              ПРОВЕРКА                               #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._settings.reputation_enabled and bool(self._settings.reputation_providers)

    @property
    def cas_size(self) -> int:
        """Сколько id в снимке CAS."""
        return len(self._cas)

    def _provider_enabled(self, name: str) -> bool:
        return name in self._settings.reputation_providers

    def in_cas(self, user_id: int) -> bool:
        """Поиск по снимку CAS делением пополам — без сети и без Redis."""
        if not self._cas:
            return False
        index = bisect.bisect_left(self._cas, user_id)
        return index < len(self._cas) and self._cas[index] == user_id

    async def check(self, user_id: int, *, online: bool = True) -> ReputationVerdict:
        """Проверяет пользователя во всех включённых источниках.

        Args:
            online: разрешить сетевой запрос к LOLS. Для «не первых» сообщений
                его выключают, чтобы не ходить в сеть на каждое сообщение.
        """
        if not self.enabled or user_id <= 0:
            return ReputationVerdict()

        if self._provider_enabled("cas_export") and self.in_cas(user_id):
            return ReputationVerdict(banned=True, source="cas")

        if not (online and self._provider_enabled("lols")):
            return ReputationVerdict()

        cached = await self._cached(user_id)
        if cached is not None:
            return cached

        verdict = await self._ask_lols(user_id)
        await self._remember(user_id, verdict)
        return verdict

    # ------------------------------------------------------------------ #
    #                               LOLS                                  #
    # ------------------------------------------------------------------ #

    async def _ask_lols(self, user_id: int) -> ReputationVerdict:
        """Онлайн-запрос к LOLS. Сетевой сбой = «чисто»."""
        try:
            session = await self._ensure_session()
            timeout = aiohttp.ClientTimeout(total=self._settings.reputation_timeout)
            async with session.get(
                LOLS_URL, params={"id": user_id}, timeout=timeout
            ) as response:
                if response.status != 200:
                    logger.debug("LOLS ответил %s для %s", response.status, user_id)
                    return ReputationVerdict()
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            logger.debug("LOLS недоступен (%s) — считаем пользователя чистым", exc)
            return ReputationVerdict()

        if not isinstance(payload, dict) or not payload.get("ok"):
            return ReputationVerdict()
        return ReputationVerdict(
            banned=bool(payload.get("banned")),
            source="lols" if payload.get("banned") else "",
            when=payload.get("when"),
        )

    def _cache_key(self, user_id: int) -> str:
        return self._settings.redis_key("rep", user_id)

    async def _cached(self, user_id: int) -> ReputationVerdict | None:
        local = self._local.get(user_id)
        if local is not None:
            return local
        if self._redis is None:
            return None
        try:
            value = await self._redis.get(self._cache_key(user_id))
        except Exception as exc:
            logger.debug("Кэш блок-листа недоступен: %s", exc)
            return None
        if value is None:
            return None
        raw = value.decode() if isinstance(value, bytes) else str(value)
        return ReputationVerdict(banned=raw == "1", source="lols-cache" if raw == "1" else "")

    async def _remember(self, user_id: int, verdict: ReputationVerdict) -> None:
        self._local.set(user_id, verdict)
        if self._redis is None:
            return
        # «Чистый» ответ живёт вчетверо меньше: статус может измениться.
        ttl = self._settings.reputation_cache_ttl
        try:
            await self._redis.set(
                self._cache_key(user_id),
                "1" if verdict.banned else "0",
                ex=ttl if verdict.banned else max(ttl // 4, 60),
            )
        except Exception as exc:
            logger.debug("Не удалось сохранить ответ блок-листа: %s", exc)

    # ------------------------------------------------------------------ #
    #                           СНИМОК CAS                                #
    # ------------------------------------------------------------------ #

    async def refresh_cas(self) -> int:
        """Скачивает экспорт CAS и заменяет снимок. Возвращает число id.

        При любой ошибке прежний снимок остаётся в силе.
        """
        if not self._provider_enabled("cas_export"):
            return 0
        try:
            session = await self._ensure_session()
            timeout = aiohttp.ClientTimeout(total=120)
            async with session.get(CAS_EXPORT_URL, timeout=timeout) as response:
                if response.status != 200:
                    logger.warning("Экспорт CAS вернул %s", response.status)
                    return len(self._cas)
                raw = await response.read()
        except (TimeoutError, aiohttp.ClientError) as exc:
            logger.warning("Не удалось скачать экспорт CAS: %s", exc)
            return len(self._cas)

        snapshot = array("q")
        for line in raw.split(b"\n"):
            line = line.strip()
            if line.isdigit():
                snapshot.append(int(line))
        if not snapshot:
            logger.warning("Экспорт CAS пуст — оставляем прежний снимок")
            return len(self._cas)

        # bisect требует отсортированных данных; порядок в выгрузке не гарантирован.
        self._cas = array("q", sorted(snapshot))
        logger.info(
            "Снимок CAS обновлён",
            extra={"component": "reputation", "cas_ids": len(self._cas)},
        )
        return len(self._cas)

    def load_cas_snapshot(self, user_ids: list[int] | set[int]) -> int:
        """Подставляет снимок CAS напрямую (прогрев из файла, тесты)."""
        self._cas = array("q", sorted(set(user_ids)))
        return len(self._cas)

    async def start_background_sync(self) -> None:
        """Запускает периодическое обновление снимка CAS."""
        if not self.enabled or not self._provider_enabled("cas_export"):
            return
        if self._sync_task is not None:
            return
        self._sync_task = asyncio.create_task(self._sync_loop(), name="cas-sync")

    async def _sync_loop(self) -> None:
        while True:
            try:
                await self.refresh_cas()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — фоновая задача не должна падать
                logger.error("Сбой синхронизации CAS: %s", exc, exc_info=True)
            await asyncio.sleep(self._settings.cas_sync_interval)

    # ------------------------------------------------------------------ #
    #                            ЖИЗНЕННЫЙ ЦИКЛ                           #
    # ------------------------------------------------------------------ #

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        """Останавливает синхронизацию и закрывает HTTP-сессию."""
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — гасим только при остановке
                logger.warning("Синхронизация CAS завершилась с ошибкой: %s", exc)
            self._sync_task = None
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()
