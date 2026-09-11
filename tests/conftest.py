"""Общая изоляция тестов от окружения разработчика.

Настройки бота читаются из `.env` и переменных окружения. Без изоляции
результат тестов зависел бы от локального конфига: понизил у себя
`DELETE_THRESHOLD` — и часть тестов «сломалась» на ровном месте.
Так и случилось однажды, поэтому изоляция закреплена здесь.

В самих тестах настройки создаются с `_env_file=None`, а тут снимаются
переменные окружения — второй канал, через который конфиг мог бы просочиться.

Очистка идёт на уровне модуля: тесты, создающие настройки в момент импорта,
до фикстур не доживают.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402


def _strip_settings_env() -> list[str]:
    """Снимает переменные окружения, совпадающие с полями настроек."""
    removed = []
    for field in Settings.model_fields:
        if os.environ.pop(field.upper(), None) is not None:
            removed.append(field.upper())
    return removed


# Часть тестов создаёт настройки прямо на уровне модуля — это происходит на
# импорте, раньше любых фикстур. Поэтому чистим окружение сразу здесь.
_STRIPPED = _strip_settings_env()


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторная очистка на случай, если переменную выставил сам тест."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
