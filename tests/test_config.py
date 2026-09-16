"""Тесты загрузки настроек именно из .env-файла.

Регрессия: pydantic-settings декодирует «сложные» поля (`set[...]`) как JSON
ещё до валидаторов, а пустая строка не приводится к `int | None`. Поэтому
конфиг обязательно проверяем файлом, а не только конструктором.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402

ENV_BODY = """
BOT_TOKEN=123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
FLIBUSTA_BOT_ID=8156231123
FLIBUSTA_USERNAME=@booksaverobot
STATIC_WHITELIST_IDS=
STATIC_WHITELIST_USERNAMES=
SUPER_ADMIN_IDS=356404555
ADMIN_LOG_CHAT_ID=
SAFE_DOMAINS=flibusta.is, litres.ru ,FantLab.ru
BAN_THRESHOLD=15.0
DRY_RUN=true
"""


def load(tmp_path: Path, body: str = ENV_BODY) -> Settings:
    env_file = tmp_path / ".env"
    env_file.write_text(body.strip() + "\n", encoding="utf-8")
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def test_empty_lists_do_not_break_loading(tmp_path: Path) -> None:
    settings = load(tmp_path)
    assert settings.static_whitelist_ids == set()
    assert settings.static_whitelist_usernames == set()


def test_empty_optional_int_becomes_none(tmp_path: Path) -> None:
    settings = load(tmp_path)
    assert settings.admin_log_chat_id is None


def test_csv_lists_are_parsed_and_normalized(tmp_path: Path) -> None:
    settings = load(tmp_path)
    assert settings.super_admin_ids == {356404555}
    assert settings.safe_domains == {"flibusta.is", "litres.ru", "fantlab.ru"}


def test_flibusta_username_loses_at_sign(tmp_path: Path) -> None:
    settings = load(tmp_path)
    assert settings.flibusta_username == "booksaverobot"
    assert settings.flibusta_bot_id == 8156231123
    assert settings.protected_ids == frozenset({8156231123, 356404555})
    assert settings.protected_usernames == frozenset({"booksaverobot"})


def test_thresholds_must_increase(tmp_path: Path) -> None:
    body = ENV_BODY.replace("BAN_THRESHOLD=15.0", "BAN_THRESHOLD=3.0")
    try:
        load(tmp_path, body)
    except Exception as exc:
        assert "delete_threshold" in str(exc)
        return
    raise AssertionError("несогласованные пороги должны отклоняться")


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            try:
                func(Path(tmp))
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"PASS {name}")
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    raise SystemExit(1 if failures else 0)
