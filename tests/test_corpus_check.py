"""Тесты регрессионного прогона выборки."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.handlers.admin_training import render_report  # noqa: E402
from bot.services.antispam_engine import AntiSpamEngine, EngineConfig  # noqa: E402
from bot.services.corpus_check import CorpusEvaluator  # noqa: E402
from bot.services.sample_service import HAM, SPAM, SampleService  # noqa: E402
from bot.tools.regress import render as render_console  # noqa: E402

CHAT_ID = -1001234567890

SPAM_TEXTS = (
    "КАЗИНО ВУЛКАН бонус 5000, регистрируйся bonus-vulkan.top/ref/12",
    "Начни зарабатывать на крипте! Переходи https://t.me/+dA3kZq1",
    "Продам аккаунты недорого, пишите",  # по тексту не ловится — это ожидаемо
)
HAM_TEXTS = (
    "Ищу «Майстеринг Биткоин» Антонопулоса, есть в fb2?",
    "Посоветуйте книги по трейдингу для начинающих",
    "Казино Рояль Флеминга кто-нибудь читал в оригинале?",
)


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", **overrides
    )


async def make_evaluator(
    spam: tuple[str, ...] = (), ham: tuple[str, ...] = ()
) -> CorpusEvaluator:
    settings = make_settings()
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    samples = SampleService(database)
    for text in spam:
        await samples.add(label=SPAM, chat_id=CHAT_ID, text=text, added_by=1)
    for text in ham:
        await samples.add(label=HAM, chat_id=CHAT_ID, text=text, added_by=1)
    return CorpusEvaluator(samples, AntiSpamEngine(EngineConfig.from_settings(settings)))


async def test_empty_corpus_is_reported_as_empty() -> None:
    report = await (await make_evaluator()).run()
    assert report.is_empty
    assert "пуста" in render_report(report)
    assert "пуста" in render_console(report)


async def test_counts_are_correct() -> None:
    report = await (await make_evaluator(SPAM_TEXTS, HAM_TEXTS)).run()
    assert report.spam_total == 3 and report.ham_total == 3
    assert report.spam_caught == 2, "третий пример по тексту не ловится"
    assert report.ham_clean == 3, "ни один книжный запрос не должен пострадать"
    assert report.false_positive_rate == 0.0
    assert round(report.spam_rate) == 67


async def test_missed_spam_is_listed() -> None:
    report = await (await make_evaluator(SPAM_TEXTS, HAM_TEXTS)).run()
    assert len(report.missed) == 1
    assert "Продам аккаунты" in report.missed[0].text
    assert not report.is_clean


async def test_false_positive_is_detected() -> None:
    """Если админ пометил как «не спам» текст, который движок ловит — это видно."""
    trap = "КАЗИНО ВУЛКАН бонус, переходи bonus.top/ref/1"
    report = await (await make_evaluator(ham=(trap,))).run()
    assert report.ham_total == 1 and report.ham_clean == 0
    assert len(report.false_positives) == 1
    assert report.false_positive_rate == 100.0
    assert report.false_positives[0].score >= 6.0


async def test_clean_corpus_is_reported_as_clean() -> None:
    report = await (await make_evaluator(SPAM_TEXTS[:2], HAM_TEXTS)).run()
    assert report.is_clean
    assert "✅" in render_report(report)


async def test_examples_are_limited() -> None:
    many = tuple(f"Продам аккаунт номер {index} недорого" for index in range(10))
    report = await (await make_evaluator(spam=many)).run(examples=3)
    assert len(report.missed) == 3
    assert report.spam_total == 10


async def test_report_mentions_numbers() -> None:
    report = await (await make_evaluator(SPAM_TEXTS, HAM_TEXTS)).run()
    html_report = render_report(report)
    console_report = render_console(report)
    for text in (html_report, console_report):
        assert "2/3" in text
        assert "Продам аккаунты" in text
    assert "<b>" in html_report and "<b>" not in console_report


async def test_run_is_reproducible() -> None:
    evaluator = await make_evaluator(SPAM_TEXTS, HAM_TEXTS)
    first = await evaluator.run()
    second = await evaluator.run()
    assert (first.spam_caught, first.ham_clean) == (second.spam_caught, second.ham_clean)


async def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            await func()
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
