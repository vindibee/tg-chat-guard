"""Ручная разметка (`/spam`, `/ham`) и жалобы участников (`/report`).

Админ, отвечающий `/spam` на сообщение, не просто банит спамера — он пополняет
выборку, по которой потом правятся правила. `/ham` работает симметрично:
снимает санкцию, помечает срабатывание ложным и сохраняет «хороший» текст.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING, Any, Final

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message

from bot.filters.admin import IsChatAdmin
from bot.services.activity_tracker import ActivityTracker
from bot.services.corpus_check import CorpusEvaluator, CorpusReport
from bot.services.event_service import EventService
from bot.services.report_service import ReportService
from bot.services.sample_service import HAM, SPAM, SampleService
from bot.utils.telegram import (
    safe_ban_member,
    safe_delete_message,
    safe_unban_member,
    safe_unrestrict_member,
)

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings
    from bot.services.admin_cache import ChatAdminCache
    from bot.services.antispam_engine import AntiSpamEngine

logger = logging.getLogger(__name__)

#: Фоновые задачи самоудаления служебных ответов.
_TASKS: Final[set[asyncio.Task[Any]]] = set()


def message_link(message: Message) -> str:
    """Ссылка на сообщение: по username чата либо по внутреннему id."""
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    internal = str(chat.id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{message.message_id}"


def _text_of(message: Message) -> str:
    return message.text or message.caption or ""


async def _reply_and_forget(message: Message, text: str, ttl: int = 15) -> None:
    """Короткий ответ, который сам исчезает и не засоряет чат."""
    try:
        notice = await message.reply(text)
    except TelegramAPIError as exc:
        logger.info("Не удалось ответить: %s", exc)
        return
    if ttl <= 0:
        return
    task = asyncio.create_task(_delete_later(notice, ttl))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _delete_later(message: Message, delay: int) -> None:
    await asyncio.sleep(delay)
    await safe_delete_message(message)


async def _notify_admins(bot: Bot, settings: Settings, text: str) -> bool:
    """Сообщение в админ-чат. `False`, если он не настроен."""
    if settings.admin_log_chat_id is None:
        return False
    try:
        await bot.send_message(settings.admin_log_chat_id, text, disable_web_page_preview=True)
        return True
    except TelegramAPIError as exc:
        logger.warning("Не удалось написать в админ-чат: %s", exc)
        return False


async def mark_spam(
    message: Message,
    bot: Bot,
    settings: Settings,
    events: EventService,
    samples: SampleService,
    activity: ActivityTracker,
    admin_cache: ChatAdminCache,
    **_: Any,
) -> None:
    """Ответом на сообщение: удалить, забанить автора и записать пример спама."""
    target = message.reply_to_message
    if target is None or target.from_user is None:
        await _reply_and_forget(message, "Ответьте командой на сообщение спамера.")
        return

    author = target.from_user
    if author.id in settings.super_admin_ids or await admin_cache.is_admin(
        bot, message.chat.id, author.id
    ):
        await _reply_and_forget(message, "Это администратор чата — команда не применяется.")
        return

    text = _text_of(target)
    deleted = await safe_delete_message(target)
    banned = await safe_ban_member(bot, message.chat.id, author.id)
    await activity.reset(message.chat.id, author.id)

    await events.record(
        chat_id=message.chat.id,
        user_id=author.id,
        username=author.username,
        message_id=target.message_id,
        action="delete_and_ban",
        reason="manual_spam",
        score=0.0,
        categories="manual",
        signals="manual",
        excerpt=text,
        deleted=deleted,
    )
    sample = await samples.add(
        label=SPAM,
        chat_id=message.chat.id,
        text=text,
        author_id=author.id,
        added_by=message.from_user.id if message.from_user else 0,
    )

    logger.info(
        "Ручная разметка спама",
        extra={
            "component": "training",
            "chat_id": message.chat.id,
            "user_id": author.id,
            "admin_id": message.from_user.id if message.from_user else 0,
            "deleted": deleted,
            "banned": banned,
            "sample_added": sample is not None,
        },
    )

    parts = ["удалено" if deleted else "удалить не вышло"]
    parts.append("автор заблокирован" if banned else "забанить не вышло")
    parts.append("пример сохранён" if sample else "такой пример уже есть")
    admin_name = html.escape(message.from_user.full_name) if message.from_user else "—"
    await _notify_admins(
        bot,
        settings,
        f"🚫 <b>/spam</b> от {admin_name}"
        f"\n{', '.join(parts)}\n\n<blockquote>{html.escape(text[:500])}</blockquote>",
    )
    await _reply_and_forget(message, f"🚫 {', '.join(parts)}.")


async def mark_ham(
    message: Message,
    bot: Bot,
    settings: Settings,
    events: EventService,
    samples: SampleService,
    **_: Any,
) -> None:
    """Ответом на сообщение: снять санкцию, пометить срабатывание ложным, сохранить пример."""
    target = message.reply_to_message
    if target is None:
        await _reply_and_forget(message, "Ответьте командой на сообщение, которое зря наказали.")
        return

    text = _text_of(target)
    author = target.from_user
    sample = await samples.add(
        label=HAM,
        chat_id=message.chat.id,
        text=text,
        author_id=author.id if author else 0,
        added_by=message.from_user.id if message.from_user else 0,
    )

    undone: list[str] = []
    event = await events.find_by_message(message.chat.id, target.message_id)
    if event is not None:
        await events.mark_reviewed(
            event.id,
            admin_id=message.from_user.id if message.from_user else 0,
            false_positive=True,
        )
        undone.append(f"событие #{event.id} помечено ложным")
        if event.action == "delete_and_ban" and event.user_id:
            if await safe_unban_member(bot, event.chat_id, event.user_id):
                undone.append("бан снят")
        elif event.action == "delete_and_mute" and event.user_id:
            if await safe_unrestrict_member(bot, event.chat_id, event.user_id):
                undone.append("мьют снят")

    logger.warning(
        "Ручная разметка ham",
        extra={
            "component": "training",
            "chat_id": message.chat.id,
            "message_id": target.message_id,
            "admin_id": message.from_user.id if message.from_user else 0,
            "event_id": event.id if event else None,
            "excerpt": text[:200],
        },
    )

    status = "пример сохранён" if sample else "такой пример уже есть"
    detail = f"{status}; {', '.join(undone)}" if undone else status
    await _reply_and_forget(message, f"✅ {detail}.")


def render_report(report: CorpusReport) -> str:
    """Собирает человекочитаемую сводку прогона."""
    if report.is_empty:
        return (
            "📊 Выборка пуста.\n\n"
            "Помечайте сообщения командами <code>/spam</code> и <code>/ham</code> "
            "в ответ — и прогон покажет, что меняется в фильтре."
        )

    total = report.spam_total + report.ham_total
    verdict_icon = "✅" if report.is_clean else "⚠️"
    lines = [
        f"{verdict_icon} <b>Прогон выборки</b> · {total} примеров за "
        f"{report.elapsed_ms:.0f} мс",
        "",
        f"Спам пойман: <b>{report.spam_caught}/{report.spam_total}</b> "
        f"({report.spam_rate:.0f}%)",
        f"Ложные срабатывания: <b>{report.ham_total - report.ham_clean}/{report.ham_total}</b> "
        f"({report.false_positive_rate:.0f}%)",
    ]

    if report.false_positives:
        lines.append("")
        lines.append("🚫 <b>Зря пойманы</b> (самые опасные промахи):")
        for outcome in report.false_positives:
            tail = f" · {html.escape(outcome.categories)}" if outcome.categories else ""
            lines.append(
                f"• <code>{outcome.score:.1f}</code>{tail} — "
                f"{html.escape(outcome.excerpt)}"
            )

    if report.missed:
        lines.append("")
        lines.append("🕳 <b>Пропущенный спам</b>:")
        for outcome in report.missed:
            lines.append(
                f"• <code>{outcome.score:.1f}</code> · "
                f"{html.escape(outcome.reason)} — {html.escape(outcome.excerpt)}"
            )

    lines.append("")
    lines.append(
        "<i>Прогон использует только текст: без блок-листов, дубликатов и профиля.</i>"
    )
    return "\n".join(lines)


async def run_regression(
    message: Message,
    samples: SampleService,
    engine: AntiSpamEngine,
    **_: Any,
) -> None:
    """Прогоняет размеченную выборку через текущие правила.

    Главный инструмент при правке словаря: видно, что именно изменилось —
    фильтр стал ловить лишнее или, наоборот, начал пропускать спам.
    """
    report = await CorpusEvaluator(samples, engine).run()
    await message.reply(render_report(report), disable_web_page_preview=True)


async def show_samples(message: Message, samples: SampleService, **_: Any) -> None:
    """Размер собранной выборки."""
    counts = await samples.counts()
    await message.reply(
        "📊 <b>Выборка</b>\n"
        f"Спам: <b>{counts.get(SPAM, 0)}</b>\n"
        f"Не спам: <b>{counts.get(HAM, 0)}</b>\n\n"
        "Пополняется командами <code>/spam</code> и <code>/ham</code> в ответ. "
        "Проверить правила на выборке: <code>/regress</code>."
    )


async def report_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    reports: ReportService,
    **_: Any,
) -> None:
    """Жалоба обычного участника. При `report_threshold` жалобах зовём админов."""
    if not settings.report_enabled or message.chat.type == ChatType.PRIVATE:
        return
    target = message.reply_to_message
    reporter = message.from_user
    if target is None or reporter is None:
        await _reply_and_forget(message, "Ответьте командой на сообщение, которое считаете спамом.")
        return
    if target.from_user is not None and target.from_user.id == reporter.id:
        await _reply_and_forget(message, "На себя жаловаться не нужно.")
        return

    count = await reports.register(message.chat.id, target.message_id, reporter.id)
    await safe_delete_message(message)
    if count == 0:
        return

    if not reports.is_enough(count):
        logger.info(
            "Жалоба принята",
            extra={
                "component": "report",
                "chat_id": message.chat.id,
                "message_id": target.message_id,
                "reports": count,
            },
        )
        return

    author = target.from_user
    name = html.escape(author.full_name) if author else "—"
    handle = f" @{html.escape(author.username)}" if author and author.username else ""
    text = (
        f"🚨 <b>Жалобы участников: {count}</b>\n"
        f"Чат: {html.escape(message.chat.title or str(message.chat.id))}\n"
        f"Автор: {name}{handle}"
        + (f" · <code>{author.id}</code>" if author else "")
        + f"\n<a href=\"{message_link(target)}\">Открыть сообщение</a>\n\n"
        f"<blockquote>{html.escape(_text_of(target)[:500])}</blockquote>\n"
        "Разобрать: ответьте на сообщение командой <code>/spam</code> или <code>/ham</code>."
    )
    logger.warning(
        "Порог жалоб достигнут",
        extra={
            "component": "report",
            "chat_id": message.chat.id,
            "message_id": target.message_id,
            "reports": count,
        },
    )
    await _notify_admins(bot, settings, text)


def build_router() -> Router:
    """Роутер ручной разметки и жалоб.

    Фильтр прав стоит на каждой команде отдельно: `/report` должен работать
    для всех участников, остальное — только для админов.
    """
    router = Router(name="admin_training")
    router.message.register(mark_spam, Command("spam"), IsChatAdmin())
    router.message.register(mark_ham, Command("ham", "notspam"), IsChatAdmin())
    router.message.register(show_samples, Command("samples", "stats"), IsChatAdmin())
    router.message.register(run_regression, Command("regress", "samples_check"), IsChatAdmin())
    router.message.register(report_message, Command("report"))
    return router
