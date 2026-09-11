"""Middleware внедрения зависимостей в хендлеры."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

__all__ = ["DependenciesMiddleware"]


class DependenciesMiddleware(BaseMiddleware):
    """Кладёт общие сервисы в `data`, чтобы хендлеры получали их как аргументы.

    Пример:
        >>> dp.update.outer_middleware(DependenciesMiddleware(whitelist=service))
        >>> async def handler(message: Message, whitelist: WhitelistService) -> None: ...
    """

    def __init__(self, **dependencies: Any) -> None:
        self._dependencies: Mapping[str, Any] = dependencies

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.update(self._dependencies)
        return await handler(event, data)
