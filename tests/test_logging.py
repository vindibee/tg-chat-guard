"""Тесты структурированного логирования.

Логи — единственный способ понять, что бот делал в проде, поэтому форматтер
проверяем отдельно: он должен переживать исключения и произвольные extra-поля.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.utils.logging import HumanFormatter, JsonFormatter, setup_logging  # noqa: E402


def make_record(message: str = "проверка", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="bot.test", level=logging.WARNING, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )
    record.__dict__.update(extra)
    return record


def test_json_line_is_valid_json() -> None:
    payload = json.loads(JsonFormatter().format(make_record()))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "bot.test"
    assert payload["message"] == "проверка"
    assert payload["ts"].endswith("+00:00")


def test_extra_fields_land_in_payload() -> None:
    line = JsonFormatter().format(make_record(chat_id=-100, verdict_score=7.5, blocklist=None))
    payload = json.loads(line)
    assert payload["chat_id"] == -100
    assert payload["verdict_score"] == 7.5
    assert payload["blocklist"] is None


def test_cyrillic_is_not_escaped() -> None:
    assert "проверка" in JsonFormatter().format(make_record())


def test_unserializable_extra_does_not_break_logging() -> None:
    """Объект без JSON-представления не должен ронять обработчик."""
    line = JsonFormatter().format(make_record(obj=object()))
    assert "object object at" in json.loads(line)["obj"]


def test_exception_is_included() -> None:
    try:
        raise ValueError("бах")
    except ValueError:
        record = make_record("упало")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: бах" in payload["exception"]


def test_human_formatter_shows_extras() -> None:
    line = HumanFormatter().format(make_record(chat_id=-100))
    assert "bot.test" in line and "chat_id" in line


def test_human_formatter_without_extras_is_plain() -> None:
    assert "|" not in HumanFormatter().format(make_record())


def test_setup_logging_replaces_handlers() -> None:
    root = logging.getLogger()
    previous = list(root.handlers)
    previous_level = root.level
    try:
        setup_logging("DEBUG", json_output=True)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.DEBUG

        setup_logging("INFO", json_output=False)
        assert isinstance(root.handlers[0].formatter, HumanFormatter)
        assert logging.getLogger("aiogram.event").level == logging.WARNING
    finally:
        root.handlers = previous
        root.setLevel(previous_level)


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
