"""Подмена aiohttp-сессии для тестов блок-листов."""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, body: bytes = b"") -> None:
        self.status = status
        self._payload = payload
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def json(self, content_type: str | None = None) -> Any:
        return self._payload

    async def read(self) -> bytes:
        return self._body


class FakeSession:
    """Отдаёт заранее заданные ответы и считает запросы."""

    def __init__(
        self,
        *,
        payload: Any = None,
        body: bytes = b"",
        status: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.body = body
        self.status = status
        self.error = error
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None) -> Any:
        self.requests.append((url, params or {}))
        if self.error is not None:
            raise self.error
        return FakeResponse(status=self.status, payload=self.payload, body=self.body)

    async def close(self) -> None:
        self.closed = True


class TimeoutSession(FakeSession):
    """Сессия, которая всегда уходит в таймаут."""

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: Any = None) -> Any:
        self.requests.append((url, params or {}))
        raise TimeoutError()
