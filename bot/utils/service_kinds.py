"""Группы служебных сообщений Telegram.

Их больше шестидесяти видов, и перечислять каждый в `.env` бессмысленно.
Вместо этого — понятные группы: `join`, `leave`, `pin` и так далее.

Платежи в список не входят ни при каких настройках: это финансовые записи,
и стирать их бот не должен.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

logger = logging.getLogger(__name__)

__all__ = ["SERVICE_GROUPS", "NEVER_DELETE", "resolve_content_types", "ALL", "NONE"]

#: Особые значения настройки.
ALL: Final[str] = "all"
NONE: Final[str] = "none"

#: Эти сообщения не удаляются даже при `all`.
NEVER_DELETE: Final[frozenset[str]] = frozenset(
    {
        # Финансовые записи.
        "successful_payment",
        "refunded_payment",
        # Переезд группы в супергруппу: на них завязана связка старого и нового чата.
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        # Ответ бота на запрос данных — это не шум, а часть диалога.
        "web_app_data",
        "users_shared",
        "user_shared",
        "chat_shared",
    }
)

SERVICE_GROUPS: Final[dict[str, frozenset[str]]] = {
    "join": frozenset({"new_chat_members", "community_chat_joined", "community_chat_added"}),
    "leave": frozenset({"left_chat_member", "chat_owner_left", "community_chat_removed"}),
    "title": frozenset({"new_chat_title", "chat_owner_changed"}),
    "photo": frozenset({"new_chat_photo", "delete_chat_photo", "chat_background_set"}),
    "pin": frozenset({"pinned_message"}),
    "videochat": frozenset(
        {
            "video_chat_scheduled",
            "video_chat_started",
            "video_chat_ended",
            "video_chat_participants_invited",
        }
    ),
    "forum": frozenset(
        {
            "forum_topic_created",
            "forum_topic_edited",
            "forum_topic_closed",
            "forum_topic_reopened",
            "general_forum_topic_hidden",
            "general_forum_topic_unhidden",
        }
    ),
    "boost": frozenset({"boost_added"}),
    "giveaway": frozenset(
        {"giveaway_created", "giveaway", "giveaway_winners", "giveaway_completed"}
    ),
    "gift": frozenset({"gift", "unique_gift", "gift_upgrade_sent"}),
    "created": frozenset(
        {"group_chat_created", "supergroup_chat_created", "channel_chat_created"}
    ),
    "other": frozenset(
        {
            "message_auto_delete_timer_changed",
            "proximity_alert_triggered",
            "write_access_allowed",
            "connected_website",
            "paid_message_price_changed",
            "direct_message_price_changed",
            "poll_option_added",
            "poll_option_deleted",
            "checklist_tasks_done",
            "checklist_tasks_added",
        }
    ),
}


def resolve_content_types(groups: Iterable[str]) -> frozenset[str]:
    """Превращает имена групп в набор content_type для фильтра aiogram.

    Неизвестные имена пропускаются с предупреждением: опечатка в `.env` не
    должна ронять бота, но и молчать о ней нельзя.
    """
    names = {str(name).strip().lower() for name in groups if str(name).strip()}
    if not names or NONE in names:
        return frozenset()

    if ALL in names:
        selected = set().union(*SERVICE_GROUPS.values())
    else:
        selected = set()
        for name in sorted(names):
            group = SERVICE_GROUPS.get(name)
            if group is None:
                logger.warning(
                    "Неизвестная группа служебных сообщений в настройках: %r. Доступны: %s",
                    name,
                    ", ".join(sorted(SERVICE_GROUPS)),
                )
                continue
            selected |= group

    return frozenset(selected - NEVER_DELETE)
