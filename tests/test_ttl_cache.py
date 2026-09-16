"""Тесты кэша в памяти: истечение и вытеснение.

Кэш используется как запасной путь при недоступности Redis, поэтому его
поведение при переполнении важно: утечка здесь означает рост памяти бота.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.utils.ttl_cache import TTLCache  # noqa: E402


def test_value_is_returned_before_expiry() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=60)
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert len(cache) == 1


def test_missing_key_is_none() -> None:
    assert TTLCache(ttl=60).get("нет такого") is None


def test_expired_value_is_dropped() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=0.2)
    cache.set("a", 1)
    time.sleep(0.3)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_setdefault_keeps_existing_value() -> None:
    cache: TTLCache[str, set[int]] = TTLCache(ttl=60)
    first = cache.setdefault("k", {1})
    first.add(2)
    assert cache.setdefault("k", set()) == {1, 2}


def test_setdefault_stores_new_value() -> None:
    cache: TTLCache[str, list[int]] = TTLCache(ttl=60)
    assert cache.setdefault("k", [7]) == [7]
    assert cache.get("k") == [7]


def test_pop_removes_value() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=60)
    cache.set("a", 1)
    assert cache.pop("a") == 1
    assert cache.pop("a") is None


def test_clear_empties_cache() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=60)
    for key in "abc":
        cache.set(key, 1)
    cache.clear()
    assert len(cache) == 0


def test_size_limit_is_respected() -> None:
    """Переполнение не должно приводить к неограниченному росту."""
    cache: TTLCache[int, int] = TTLCache(ttl=60, maxsize=5)
    for key in range(50):
        cache.set(key, key)
    assert len(cache) <= 5


def test_oldest_entries_are_evicted_first() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=60, maxsize=2)
    cache.set("старый", 1)
    cache.set("средний", 2)
    cache.set("новый", 3)
    assert cache.get("старый") is None
    assert cache.get("новый") == 3


def test_expired_entries_are_reclaimed_on_overflow() -> None:
    """Сначала выбрасывается протухшее, живые записи переживают переполнение."""
    cache: TTLCache[str, int] = TTLCache(ttl=0.2, maxsize=3)
    for key in ("a", "b", "c"):
        cache.set(key, 1)
    time.sleep(0.3)
    cache.set("свежий", 2)
    assert cache.get("свежий") == 2
    assert len(cache) == 1


def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())
