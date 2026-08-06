from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hugin.domain.content import MessageDirection


class HhSyncBlockedError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HhSyncRetryableError(RuntimeError):
    def __init__(self, code: str, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.code = code.strip()[:64] or "HH_TEMPORARY_LIMIT"
        self.retry_after_seconds = max(1, retry_after_seconds)


class HhNegotiationStatus(StrEnum):
    APPLIED = "APPLIED"
    VIEWED = "VIEWED"
    INVITED = "INVITED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class HhNegotiationData:
    vacancy_id: str
    status: HhNegotiationStatus
    status_label: str
    chat_available: bool = False


@dataclass(frozen=True, slots=True)
class HhChatMessageData:
    vacancy_id: str
    hh_id: str
    direction: MessageDirection
    body: str
    displayed_time: str = ""

    def __post_init__(self) -> None:
        if not self.vacancy_id or len(self.vacancy_id) > 64:
            raise ValueError("Некорректный идентификатор вакансии сообщения")
        if not self.hh_id or len(self.hh_id) > 128:
            raise ValueError("Некорректный идентификатор сообщения hh.ru")
        if not self.body.strip():
            raise ValueError("Пустое сообщение hh.ru")
