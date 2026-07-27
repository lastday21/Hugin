from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import ApplicationModel, VacancyModel
from hugin.domain.applications import ApplicationState
from hugin.domain.automation import AutomationJobResult
from hugin.domain.content import MessageDirection
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhNegotiationStatus,
)
from hugin.domain.state_machines import APPLICATION_TRANSITIONS
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.communications import CommunicationRepository

_ATTENTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:приглаша\w*|собеседован\w*|интервью|встреч\w*|созвон\w*)\b", re.I),
        "Приглашение на собеседование",
    ),
    (
        re.compile(r"\b(?:тестов\w*|тестовое|задани\w*|опрос\w*)\b", re.I),
        "Задание от работодателя",
    ),
    (
        re.compile(r"\b(?:вопрос\w*|уточнит\w*|ответьте|сообщите|напишите)\b", re.I),
        "Вопрос работодателя",
    ),
)
_HTTPS_URL = re.compile(r"https://[^\s<>\"]+", re.I)


class HhSynchronizationService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._applications = ApplicationRepository(session)
        self._communications = CommunicationRepository(session)

    def tracked_vacancy_ids(self, account_id: int) -> tuple[str, ...]:
        return tuple(self._application_map(account_id))

    def synchronize_statuses(
        self,
        *,
        account_id: int,
        statuses: tuple[HhNegotiationData, ...],
        checked_at: datetime | None = None,
    ) -> AutomationJobResult:
        selected_at = checked_at or datetime.now(UTC)
        applications = self._application_map(account_id)
        updated = 0
        invitations = 0
        matched: set[str] = set()

        for item in statuses:
            application = applications.get(item.vacancy_id)
            if application is None:
                continue
            matched.add(item.vacancy_id)
            target = ApplicationState(item.status.value)
            current = self._applications.get(application.id)
            if current.state is ApplicationState.APPLYING:
                current = self._applications.transition_state(
                    current.id,
                    ApplicationState.APPLIED,
                    {
                        "hh_status": HhNegotiationStatus.APPLIED.value,
                        "source": "hh.ru",
                        "status_label": item.status_label[:255],
                    },
                )
                updated += 1
            if target is not current.state and target in APPLICATION_TRANSITIONS[current.state]:
                self._applications.transition_state(
                    current.id,
                    target,
                    {
                        "hh_status": item.status.value,
                        "source": "hh.ru",
                        "status_label": item.status_label[:255],
                    },
                )
                updated += 1
            if item.status is HhNegotiationStatus.INVITED:
                self._communications.save_invitation(
                    application_id=current.id,
                    hh_id=f"status:{item.vacancy_id}:invited",
                    title="Приглашение на собеседование",
                    details=item.status_label or None,
                    interview_at=None,
                    booking_url=None,
                    updated_at=selected_at,
                )
                invitations += 1

        return {
            "tracked": len(applications),
            "received": len(statuses),
            "matched": len(matched),
            "updated": updated,
            "invitations": invitations,
        }

    def synchronize_messages(
        self,
        *,
        account_id: int,
        messages: tuple[HhChatMessageData, ...],
        checked_at: datetime | None = None,
    ) -> AutomationJobResult:
        selected_at = checked_at or datetime.now(UTC)
        applications = self._application_map(account_id)
        created = 0
        incoming = 0
        outgoing = 0
        attention = 0
        matched: set[str] = set()

        for item in messages:
            application = applications.get(item.vacancy_id)
            if application is None:
                continue
            matched.add(item.vacancy_id)
            _record, is_new = self._communications.save_synced_message(
                application_id=application.id,
                hh_id=item.hh_id,
                direction=item.direction,
                body=item.body,
                occurred_at=selected_at,
            )
            if not is_new:
                continue
            created += 1
            if item.direction is MessageDirection.OUTGOING:
                outgoing += 1
                continue
            incoming += 1
            attention_title = self._attention_title(item.body)
            if attention_title is None:
                continue
            self._communications.save_invitation(
                application_id=application.id,
                hh_id=f"message:{item.hh_id}",
                title=attention_title,
                details=item.body,
                interview_at=None,
                booking_url=self._booking_url(item.body),
                updated_at=selected_at,
            )
            attention += 1

        return {
            "tracked": len(applications),
            "received": len(messages),
            "matched": len(matched),
            "created": created,
            "incoming": incoming,
            "outgoing": outgoing,
            "attention": attention,
        }

    def _application_map(self, account_id: int) -> dict[str, ApplicationModel]:
        if account_id < 1:
            raise ValueError("Идентификатор аккаунта должен быть положительным")
        rows = self._session.execute(
            select(ApplicationModel, VacancyModel.hh_id)
            .join(VacancyModel, VacancyModel.id == ApplicationModel.vacancy_id)
            .where(ApplicationModel.account_id == account_id)
            .order_by(ApplicationModel.id.desc())
        )
        applications: dict[str, ApplicationModel] = {}
        for application, vacancy_hh_id in rows:
            applications.setdefault(vacancy_hh_id, application)
        return applications

    @staticmethod
    def _attention_title(body: str) -> str | None:
        return next(
            (title for pattern, title in _ATTENTION_PATTERNS if pattern.search(body)),
            None,
        )

    @staticmethod
    def _booking_url(body: str) -> str | None:
        match = _HTTPS_URL.search(body)
        return match.group(0).rstrip(".,;:!?)»") if match is not None else None
