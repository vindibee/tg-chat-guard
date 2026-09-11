"""Минимальная подмена `aiogram.Bot` для тестов без сети."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aiogram.types import Chat, ChatMemberOwner, Message, User


class FakeBot:
    """Фиксирует вызовы вместо обращения к Telegram."""

    def __init__(self, admin_ids: tuple[int, ...] = ()) -> None:
        self.id = 424242
        self.admin_ids = admin_ids
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def called(self, name: str) -> bool:
        return any(call == name for call, _ in self.calls)

    def payload(self, name: str) -> dict[str, Any]:
        for call, kwargs in self.calls:
            if call == name:
                return kwargs
        return {}

    # --- вызов method-объектов aiogram (message.answer, callback.answer) ---
    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        self._record(type(method).__name__, **method.model_dump(exclude_none=True))
        return True

    # --- методы Bot API, которые дёргает код модерации ---
    async def get_chat_administrators(self, chat_id: int) -> list[ChatMemberOwner]:
        self._record("get_chat_administrators", chat_id=chat_id)
        return [
            ChatMemberOwner(
                user=User(id=admin_id, is_bot=False, first_name="Admin"),
                status="creator",
                is_anonymous=False,
            )
            for admin_id in self.admin_ids
        ]

    async def unban_chat_member(
        self, chat_id: int, user_id: int, only_if_banned: bool = False
    ) -> bool:
        self._record("unban_chat_member", chat_id=chat_id, user_id=user_id)
        return True

    async def restrict_chat_member(self, **kwargs: Any) -> bool:
        self._record("restrict_chat_member", **kwargs)
        return True

    async def ban_chat_member(self, **kwargs: Any) -> bool:
        self._record("ban_chat_member", **kwargs)
        return True

    async def ban_chat_sender_chat(self, chat_id: int, sender_chat_id: int) -> bool:
        self._record("ban_chat_sender_chat", chat_id=chat_id, sender_chat_id=sender_chat_id)
        return True

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        self._record("get_chat", chat_id=chat_id)
        return SimpleNamespace(permissions=None)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        self._record("send_message", chat_id=chat_id, text=text, **kwargs)
        return Message(
            message_id=1,
            date=datetime.now(tz=UTC),
            chat=Chat(id=chat_id, type="supergroup"),
            text=text,
        )
