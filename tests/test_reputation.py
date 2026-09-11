"""Тесты внешних блок-листов: снимок CAS, онлайн-запрос LOLS, деградация."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from tests.fake_http import FakeSession, TimeoutSession  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

BANNED_ID = 8173906631
CLEAN_ID = 356404555


def make_settings(**overrides: Any) -> Settings:
    return Settings(bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", **overrides)


# ------------------------------- снимок CAS --------------------------------


async def test_cas_snapshot_is_sorted_and_searchable() -> None:
    export = chr(10).join(["8759811393", "8173906631", "7181254393", "", "не число"]).encode()
    service = ReputationService(make_settings(), None, FakeSession(body=export))
    assert await service.refresh_cas() == 3
    assert service.in_cas(BANNED_ID)
    assert service.in_cas(7181254393)
    assert not service.in_cas(CLEAN_ID)


async def test_broken_export_keeps_previous_snapshot() -> None:
    service = ReputationService(make_settings(), None, FakeSession(body=b"8173906631\n"))
    await service.refresh_cas()
    service._session = FakeSession(status=503)  # noqa: SLF001 - проверяем деградацию
    assert await service.refresh_cas() == 1
    assert service.in_cas(BANNED_ID), "старый снимок должен уцелеть"


async def test_cas_hit_does_not_touch_network() -> None:
    session = FakeSession(body=b"8173906631\n")
    service = ReputationService(make_settings(), None, session)
    await service.refresh_cas()
    session.requests.clear()
    verdict = await service.check(BANNED_ID)
    assert verdict.banned and verdict.source == "cas"
    assert session.requests == [], "снимок локальный, запросов быть не должно"


# ---------------------------------- LOLS -----------------------------------


async def test_lols_ban_is_detected() -> None:
    session = FakeSession(
        payload={"ok": True, "user_id": BANNED_ID, "banned": True, "when": "2025-02-12"}
    )
    service = ReputationService(make_settings(reputation_providers="lols"), None, session)
    verdict = await service.check(BANNED_ID)
    assert verdict.banned and verdict.source == "lols" and verdict.when == "2025-02-12"
    assert session.requests[0][1] == {"id": BANNED_ID}


async def test_clean_user_passes() -> None:
    session = FakeSession(payload={"ok": True, "user_id": CLEAN_ID, "banned": False})
    service = ReputationService(make_settings(reputation_providers="lols"), None, session)
    assert not (await service.check(CLEAN_ID)).banned


async def test_answer_is_cached_in_redis() -> None:
    redis = FakeRedis()
    session = FakeSession(payload={"ok": True, "banned": True})
    service = ReputationService(make_settings(reputation_providers="lols"), redis, session)
    await service.check(BANNED_ID)
    await service.check(BANNED_ID)
    assert len(session.requests) == 1, "второй раз должен отвечать кэш"


async def test_offline_mode_skips_network() -> None:
    session = FakeSession(payload={"ok": True, "banned": True})
    service = ReputationService(make_settings(reputation_providers="lols"), None, session)
    assert not (await service.check(BANNED_ID, online=False)).banned
    assert session.requests == []


# ------------------------------ деградация ---------------------------------


async def test_timeout_means_clean() -> None:
    service = ReputationService(
        make_settings(reputation_providers="lols"), None, TimeoutSession()
    )
    assert not (await service.check(BANNED_ID)).banned


async def test_http_error_means_clean() -> None:
    service = ReputationService(
        make_settings(reputation_providers="lols"), None, FakeSession(status=500)
    )
    assert not (await service.check(BANNED_ID)).banned


async def test_disabled_service_never_checks() -> None:
    session = FakeSession(payload={"ok": True, "banned": True})
    service = ReputationService(make_settings(reputation_enabled=False), None, session)
    assert not service.enabled
    assert not (await service.check(BANNED_ID)).banned
    assert session.requests == []


async def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            result = func()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
