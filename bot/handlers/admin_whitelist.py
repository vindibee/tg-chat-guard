"""Админский интерфейс управления белым списком и отладки правил."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.db.models import GLOBAL_SCOPE
from bot.filters.admin import IsChatAdmin
from bot.services.antispam_engine import AntiSpamEngine, MessageContext
from bot.services.whitelist_service import WhitelistError, WhitelistService

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

logger = logging.getLogger(__name__)

_GLOBAL_FLAGS = {"--global", "-g", "глобально"}
#: Формат username в Telegram.
_USERNAME_RE = re.compile(r"[a-z0-9_]{5,32}")

_HELP = (
    "🛡 <b>Управление белым списком</b>\n\n"
    "<code>/whitelist_add</code> — ответом на сообщение пользователя\n"
    "<code>/whitelist_add @username [причина]</code>\n"
    "<code>/whitelist_add 123456789 [причина]</code>\n"
    "<code>/whitelist_remove @username|id</code>\n"
    "<code>/whitelist_list</code> — показать текущие правила\n"
    "<code>/spamcheck текст</code> — проверить текст движком\n\n"
    "Флаг <code>--global</code> применяет правило ко всем чатам "
    "(только для суперадминов бота)."
)


@dataclass(frozen=True, slots=True)
class WhitelistTarget:
    """Разобранная цель админской команды."""

    user_id: int | None
    username: str | None
    is_bot: bool
    scope: int
    reason: str | None

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else f"id={self.user_id}"


def parse_target(
    message: Message, command: CommandObject, settings: Settings
) -> WhitelistTarget:
    """Разбирает цель команды: ответ на сообщение либо аргументы.

    Raises:
        WhitelistError: цель не определена или не хватает прав на `--global`.
    """
    tokens = (command.args or "").split()
    is_global = any(token.lower() in _GLOBAL_FLAGS for token in tokens)
    tokens = [token for token in tokens if token.lower() not in _GLOBAL_FLAGS]

    user = message.from_user
    if is_global and (user is None or user.id not in settings.super_admin_ids):
        raise WhitelistError("Флаг --global доступен только суперадминам бота.")
    scope = GLOBAL_SCOPE if is_global else message.chat.id

    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        target_user = reply.from_user
        return WhitelistTarget(
            user_id=target_user.id,
            username=(target_user.username or "").lower() or None,
            is_bot=target_user.is_bot,
            scope=scope,
            reason=" ".join(tokens) or None,
        )

    if not tokens:
        raise WhitelistError("Укажите @username, id или ответьте на сообщение пользователя.")

    raw_target, *rest = tokens
    reason = " ".join(rest) or None
    if raw_target.lstrip("-").isdigit():
        return WhitelistTarget(
            user_id=int(raw_target), username=None, is_bot=False, scope=scope, reason=reason
        )

    username = raw_target.lstrip("@").lower()
    # Telegram разрешает только латиницу, цифры и подчёркивание, 5–32 символа.
    # Без этой проверки в список попадали кириллические «юзернеймы», которые
    # не совпадут ни с одним реальным участником.
    if not _USERNAME_RE.fullmatch(username):
        raise WhitelistError(
            f"Некорректный username: {raw_target!r}. "
            "Ожидается латиница, цифры и подчёркивание, 5–32 символа."
        )
    return WhitelistTarget(
        user_id=None,
        username=username,
        is_bot=username.endswith("bot"),
        scope=scope,
        reason=reason,
    )


async def show_help(message: Message, **_: Any) -> None:
    """Справка по командам белого списка."""
    await message.reply(_HELP)


async def whitelist_add(
    message: Message,
    command: CommandObject,
    settings: Settings,
    whitelist: WhitelistService,
    **_: Any,
) -> None:
    """Добавляет пользователя или бота в белый список чата (или глобально)."""
    user = message.from_user
    try:
        target = parse_target(message, command, settings)
        entry = await whitelist.add(
            chat_id=target.scope,
            user_id=target.user_id,
            username=target.username,
            added_by=user.id if user else 0,
            reason=target.reason,
            is_bot=target.is_bot,
        )
    except WhitelistError as exc:
        await message.reply(f"⚠️ {html.escape(str(exc))}")
        return

    scope_label = "во всех чатах" if entry.is_global else "в этом чате"
    await message.reply(
        f"✅ {html.escape(target.label)} добавлен в белый список {scope_label}."
    )


async def whitelist_remove(
    message: Message,
    command: CommandObject,
    settings: Settings,
    whitelist: WhitelistService,
    **_: Any,
) -> None:
    """Убирает пользователя или бота из белого списка."""
    try:
        target = parse_target(message, command, settings)
        removed = await whitelist.remove(
            chat_id=target.scope, user_id=target.user_id, username=target.username
        )
    except WhitelistError as exc:
        await message.reply(f"⚠️ {html.escape(str(exc))}")
        return

    if not removed:
        await message.reply(f"ℹ️ {html.escape(target.label)} в белом списке не найден.")
        return
    await message.reply(f"🗑 {html.escape(target.label)} удалён из белого списка.")


async def whitelist_list(
    message: Message, settings: Settings, whitelist: WhitelistService, **_: Any
) -> None:
    """Показывает статические и динамические правила, действующие в чате."""
    try:
        entries = await whitelist.list_entries(message.chat.id)
    except WhitelistError as exc:
        await message.reply(f"⚠️ {html.escape(str(exc))}")
        return

    static_ids = ", ".join(str(i) for i in sorted(settings.protected_ids)) or "—"
    static_names = ", ".join(f"@{n}" for n in sorted(settings.protected_usernames)) or "—"
    lines = [
        "🛡 <b>Белый список</b>",
        "",
        "<b>Статический (.env, изменяется только деплоем):</b>",
        f"id: <code>{html.escape(static_ids)}</code>",
        f"username: <code>{html.escape(static_names)}</code>",
        "",
        "<b>Динамический (управляется админами):</b>",
    ]
    if entries:
        lines.extend(f"• {html.escape(entry.describe())}" for entry in entries)
    else:
        lines.append("— пусто —")
    await message.reply("\n".join(lines))


async def spam_check(
    message: Message,
    command: CommandObject,
    engine: AntiSpamEngine,
    settings: Settings,
    **_: Any,
) -> None:
    """Прогоняет произвольный текст через движок и показывает разбор.

    Помогает настраивать правила без экспериментов на живом чате.
    """
    reply = message.reply_to_message
    text = command.args or (reply.text or reply.caption if reply else None) or ""
    if not text.strip():
        await message.reply(
            "Использование: <code>/spamcheck текст</code> или ответом на сообщение."
        )
        return

    # При ответе на сообщение проверяем и профиль его автора: реклама часто
    # вынесена именно в имя, а текст оставлен безобидным.
    author_name = ""
    author_username = ""
    if reply is not None and reply.from_user is not None:
        author_name = reply.from_user.full_name
        author_username = (reply.from_user.username or "").lower()

    verdict = engine.evaluate(
        MessageContext(
            text=text,
            known_safe_usernames=settings.protected_usernames,
            author_name=author_name,
            author_username=author_username,
        )
    )
    verdict_icon = "🚫" if verdict.is_spam else "✅"
    signals = "\n".join(f"• {signal}" for signal in verdict.signals) or "• признаков нет"
    await message.reply(
        f"{verdict_icon} <b>{verdict.action.value}</b> "
        f"(скор {verdict.score:.2f}, причина: <code>{verdict.reason.value}</code>)\n"
        f"Книжный запрос: {'да' if verdict.book_intent else 'нет'}\n"
        f"Профиль: {html.escape(author_name) if author_name else '—'}\n\n"
        f"<b>Признаки:</b>\n<code>{html.escape(signals)}</code>"
    )


async def antispam_status(
    message: Message, settings: Settings, engine: AntiSpamEngine, **_: Any
) -> None:
    """Текущие пороги и режим работы фильтра."""
    config = engine.config
    await message.reply(
        "🛡 <b>Антиспам</b>\n"
        f"Режим: {'наблюдение (dry-run)' if settings.dry_run else 'активный'}\n"
        f"Пороги: удаление {config.delete_threshold:g} / "
        f"мьют {config.mute_threshold:g} / бан {config.ban_threshold:g}\n"
        f"Требовать спам-фактор: {'да' if config.require_spam_factor else 'нет'}\n"
        f"Мьют: {settings.mute_duration} сек.\n"
        f"Доверенных доменов: {len(config.safe_domains)}"
    )


def build_router() -> Router:
    """Роутер админских команд белого списка.

    Фильтр прав вешается на роутер целиком: ни одна команда отсюда не должна
    быть доступна обычному участнику.
    """
    router = Router(name="admin_whitelist")
    router.message.filter(IsChatAdmin())
    router.message.register(show_help, Command("whitelist_help", "whitelist"))
    router.message.register(whitelist_add, Command("whitelist_add"))
    router.message.register(whitelist_remove, Command("whitelist_remove", "whitelist_del"))
    router.message.register(whitelist_list, Command("whitelist_list", "whitelist_show"))
    router.message.register(spam_check, Command("spamcheck", "check"))
    router.message.register(antispam_status, Command("antispam_status", "status"))
    return router
