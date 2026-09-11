"""Маленький кэш в памяти процесса с временем жизни записей.

Нужен как запасной путь, когда Redis недоступен или не настроен: без него
сервисы либо ходят в сеть на каждое сообщение, либо теряют состояние совсем.
"""

from __future__ import annotations

import time
from typing import Generic, TypeVar

__all__ = ["TTLCache"]

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Словарь с истечением записей и ограничением размера.

    Вытеснение ленивое: просроченные записи убираются при обращении, а при
    переполнении удаляются самые старые. Для объёмов бота этого достаточно,
    отдельный поток очистки не нужен.
    """

    def __init__(self, ttl: float, maxsize: int = 10_000) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._data: dict[K, tuple[float, V]] = {}

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: K) -> V | None:
        """Значение или `None`, если его нет либо срок истёк."""
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: K, value: V) -> V:
        """Кладёт значение и продлевает срок жизни."""
        if len(self._data) >= self._maxsize:
            self._evict()
        self._data[key] = (time.monotonic() + self._ttl, value)
        return value

    def setdefault(self, key: K, default: V) -> V:
        """Возвращает существующее значение либо кладёт и возвращает новое."""
        current = self.get(key)
        if current is not None:
            return current
        return self.set(key, default)

    def pop(self, key: K) -> V | None:
        item = self._data.pop(key, None)
        return None if item is None else item[1]

    def clear(self) -> None:
        self._data.clear()

    def _evict(self) -> None:
        """Чистит просроченное, а если не помогло — самые старые записи."""
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._data.items() if expires_at <= now]
        for key in expired:
            self._data.pop(key, None)
        overflow = len(self._data) - self._maxsize + 1
        if overflow > 0:
            oldest = sorted(self._data, key=lambda key: self._data[key][0])[:overflow]
            for key in oldest:
                self._data.pop(key, None)
