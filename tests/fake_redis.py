"""Минимальный асинхронный фейк Redis для тестов (SET + строки + pipeline)."""

from __future__ import annotations

from typing import Any


class FakeRedisError(RuntimeError):
    """Имитация сетевой ошибки Redis."""


class FakePipeline:
    def __init__(self, store: dict[str, Any], fail: bool) -> None:
        self._store = store
        self._fail = fail
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def exists(self, key: str) -> None:
        self._ops.append(("exists", (key,)))

    def smembers(self, key: str) -> None:
        self._ops.append(("smembers", (key,)))

    def sadd(self, key: str, *values: Any) -> None:
        self._ops.append(("sadd", (key, *values)))

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", (key, ttl)))

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self._ops.append(("set", (key, value)))

    def incr(self, key: str) -> None:
        self._ops.append(("incr", (key,)))

    def get(self, key: str) -> None:
        self._ops.append(("get", (key,)))

    def delete(self, *keys: str) -> None:
        self._ops.append(("delete", keys))

    async def execute(self) -> list[Any]:
        if self._fail:
            raise FakeRedisError("redis is down")
        results: list[Any] = []
        for op, args in self._ops:
            if op == "exists":
                results.append(1 if args[0] in self._store else 0)
            elif op == "smembers":
                results.append(set(self._store.get(args[0], set())))
            elif op == "sadd":
                bucket = self._store.setdefault(args[0], set())
                bucket.update(str(value) for value in args[1:])
                results.append(len(args) - 1)
            elif op == "get":
                value = self._store.get(args[0])
                results.append(None if value is None else str(value))
            elif op == "incr":
                self._store[args[0]] = int(self._store.get(args[0], 0)) + 1
                results.append(self._store[args[0]])
            elif op == "set":
                self._store[args[0]] = args[1]
                results.append(True)
            elif op == "delete":
                for key in args[0]:
                    self._store.pop(key, None)
                results.append(True)
            else:  # expire и прочее — no-op
                results.append(True)
        return results


class FakeRedis:
    """Подмена `redis.asyncio.Redis` с переключаемым отказом."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, Any] = {}
        self.fail = fail

    def pipeline(self, transaction: bool = False) -> FakePipeline:
        return FakePipeline(self.store, self.fail)

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise FakeRedisError("redis is down")
        value = self.store.get(key)
        return None if value is None else str(value)

    async def set(self, key: str, value: object, ex: int | None = None) -> bool:
        if self.fail:
            raise FakeRedisError("redis is down")
        self.store[key] = value
        return True

    async def sadd(self, key: str, *values: object) -> int:
        if self.fail:
            raise FakeRedisError("redis is down")
        bucket = self.store.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(value) for value in values)
        return len(bucket) - before

    async def scard(self, key: str) -> int:
        if self.fail:
            raise FakeRedisError("redis is down")
        return len(self.store.get(key, set()))

    async def expire(self, key: str, ttl: int) -> bool:
        if self.fail:
            raise FakeRedisError("redis is down")
        return True

    async def delete(self, *keys: str) -> int:
        if self.fail:
            raise FakeRedisError("redis is down")
        removed = 0
        for key in keys:
            removed += 1 if self.store.pop(key, None) is not None else 0
        return removed

    async def aclose(self) -> None:
        return None
