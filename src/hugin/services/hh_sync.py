from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import ApplicationModel, ApplicationTaskModel, VacancyModel
from hugin.domain.applications import ApplicationState, EventPayload
from hugin.domain.automation import AutomationJobResult
from hugin.domain.content import MessageDirection, RecruiterMessageState
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhNegotiationStatus,
)
from hugin.domain.state_machines import APPLICATION_TRANSITIONS
from hugin.domain.tasks import TaskState
from hugin.repositories.applications import ApplicationRepository
from hugin.repositories.communications import CommunicationRepository
from hugin.repositories.tasks import QueueTaskRepository
from hugin.services.incidents import IncidentService
from hugin.services.screening_forms import ScreeningDraftService

_INTERVIEW_TITLE = "Приглашение на собеседование"
_NEGATED_ATTENTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bне\s+"  # noqa: RUF001
        r"(?:(?:готов|мож|смож|буд|планир|хот)\w*\s+){0,2}"
        r"(?:(?:пока|сейчас|далее|больше)\s+)?"
        r"(?:приглас|приглаш)\w*",
        re.I,
    ),
    re.compile(
        r"\bне\s+"  # noqa: RUF001
        r"(?:(?:готов|буд|планир)\w*\s+){0,2}"
        r"(?:провод\w+\s+)?"
        r"(?:собеседован|интервью|встреч|созвон)\w*",
        re.I,
    ),
    re.compile(
        r"\b(?:собеседован|интервью|встреч|созвон)\w*"
        r"\s+не\s+(?:буд|состо)\w*",
        re.I,
    ),
)
_ATTENTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:тестов\w*|тестовое|задани\w*|опрос\w*)\b", re.I),
        "Задание от работодателя",
    ),
    (
        re.compile(r"\b(?:приглаша\w*|собеседован\w*|интервью|встреч\w*|созвон\w*)\b", re.I),
        _INTERVIEW_TITLE,
    ),
    (
        re.compile(r"\b(?:вопрос\w*|уточнит\w*|ответьте|сообщите|напишите)\b", re.I),
        "Вопрос работодателя",
    ),
)
_HTTPS_URL = re.compile(r"https://[^\s<>\"]+", re.I)


@dataclass(frozen=True, slots=True)
class MessageSynchronizationResult:
    metrics: AutomationJobResult
    new_incoming_message_ids: tuple[int, ...]


class HhSynchronizationService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._applications = ApplicationRepository(session)
        self._communications = CommunicationRepository(session)
        self._tasks = QueueTaskRepository(session)
        self._incidents = IncidentService(session)

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
        manual_reconciliation = self._ambiguous_unknown_result_vacancies(account_id)
        updated = 0
        invitations = 0
        matched: set[str] = set()

        for item in statuses:
            application = applications.get(item.vacancy_id)
            if application is None or item.vacancy_id in manual_reconciliation:
                continue
            matched.add(item.vacancy_id)
            target = ApplicationState(item.status.value)
            current = self._applications.get(application.id)
            task = self._tasks.get_by_application_id(application.id)
            event_source = (
                "hugin_reconciliation"
                if task is not None and task.state is TaskState.UNKNOWN_RESULT
                else "hh.ru"
            )
            if current.state is ApplicationState.APPLYING:
                applied_payload: EventPayload = {
                    "hh_status": HhNegotiationStatus.APPLIED.value,
                    "source": event_source,
                    "status_label": item.status_label[:255],
                }
                if event_source == "hugin_reconciliation" and task is not None:
                    applied_payload["task_id"] = task.id
                current = self._applications.transition_state(
                    current.id,
                    ApplicationState.APPLIED,
                    applied_payload,
                )
                updated += 1
            if target is not current.state and target in APPLICATION_TRANSITIONS[current.state]:
                current = self._applications.transition_state(
                    current.id,
                    target,
                    {
                        "hh_status": item.status.value,
                        "source": "hh.ru",
                        "status_label": item.status_label[:255],
                    },
                )
                updated += 1
            ScreeningDraftService(self._session).mark_sent(
                current.id,
                sent_at=selected_at,
            )
            self._close_obsolete_task(current.id, item)
            if item.status is HhNegotiationStatus.INVITED:
                if self._communications.has_open_message_invitation(
                    current.id,
                    _INTERVIEW_TITLE,
                ):
                    self._communications.close_status_invitations(current.id, selected_at)
                else:
                    self._communications.save_invitation(
                        application_id=current.id,
                        hh_id=f"status:{item.vacancy_id}:invited",
                        title=_INTERVIEW_TITLE,
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

    def _close_obsolete_task(
        self,
        application_id: int,
        status: HhNegotiationData,
    ) -> None:
        task = self._tasks.get_by_application_id(application_id)
        if task is None:
            return
        payload: EventPayload = {
            "source": "hh.ru",
            "hh_status": status.status.value,
            "status_label": status.status_label[:255],
        }
        if task.state in {
            TaskState.PENDING,
            TaskState.RETRY_SCHEDULED,
            TaskState.REVIEW_REQUIRED,
            TaskState.INPUT_REQUIRED,
        }:
            self._tasks.transition(
                task.id,
                TaskState.SKIPPED,
                error_code="ALREADY_APPLIED_ON_HH",
                event_payload=payload,
            )
        elif task.state in {TaskState.RUNNING, TaskState.UNKNOWN_RESULT}:
            self._tasks.transition(
                task.id,
                TaskState.COMPLETED,
                event_payload=payload,
            )

    def synchronize_messages(
        self,
        *,
        account_id: int,
        messages: tuple[HhChatMessageData, ...],
        checked_at: datetime | None = None,
    ) -> AutomationJobResult:
        return self.synchronize_messages_with_new_ids(
            account_id=account_id,
            messages=messages,
            checked_at=checked_at,
        ).metrics

    def synchronize_messages_with_new_ids(
        self,
        *,
        account_id: int,
        messages: tuple[HhChatMessageData, ...],
        checked_at: datetime | None = None,
    ) -> MessageSynchronizationResult:
        selected_at = checked_at or datetime.now(UTC)
        applications = self._application_map(account_id)
        created = 0
        incoming = 0
        outgoing = 0
        attention = 0
        matched: set[str] = set()
        new_incoming_message_ids: list[int] = []

        for item in messages:
            application = applications.get(item.vacancy_id)
            if application is None:
                continue
            matched.add(item.vacancy_id)
            record, is_new = self._communications.save_synced_message(
                application_id=application.id,
                hh_id=item.hh_id,
                direction=item.direction,
                body=item.body,
                occurred_at=selected_at,
            )
            if is_new:
                created += 1
            if item.direction is MessageDirection.OUTGOING:
                if record.state is RecruiterMessageState.SENT:
                    self._communications.complete_reply_action_for_sent_outgoing(
                        account_id=account_id,
                        message_id=record.id,
                        completed_at=record.sent_at or selected_at,
                    )
                    for code in (
                        "RECRUITER_MESSAGE_SEND_FAILED",
                        "RECRUITER_MESSAGE_SEND_UNKNOWN",
                    ):
                        self._incidents.resolve(
                            code=code,
                            scope_type="recruiter_message",
                            scope_id=record.id,
                        )
                if is_new:
                    outgoing += 1
                continue
            if is_new:
                incoming += 1
                new_incoming_message_ids.append(record.id)
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
            if attention_title == _INTERVIEW_TITLE:
                self._communications.close_status_invitations(application.id, selected_at)
            if is_new:
                attention += 1

        return MessageSynchronizationResult(
            metrics={
                "tracked": len(applications),
                "received": len(messages),
                "matched": len(matched),
                "created": created,
                "incoming": incoming,
                "outgoing": outgoing,
                "attention": attention,
            },
            new_incoming_message_ids=tuple(new_incoming_message_ids),
        )

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

    def _ambiguous_unknown_result_vacancies(self, account_id: int) -> frozenset[str]:
        rows = self._session.execute(
            select(
                VacancyModel.hh_id,
                ApplicationModel.resume_id,
                ApplicationTaskModel.state,
            )
            .join(ApplicationModel, ApplicationModel.vacancy_id == VacancyModel.id)
            .outerjoin(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .where(ApplicationModel.account_id == account_id)
            .order_by(ApplicationModel.id.desc())
        )
        resumes_by_vacancy: dict[str, set[int]] = {}
        has_unknown_result: set[str] = set()
        for vacancy_hh_id, resume_id, task_state in rows:
            resumes_by_vacancy.setdefault(vacancy_hh_id, set()).add(resume_id)
            if task_state is TaskState.UNKNOWN_RESULT:
                has_unknown_result.add(vacancy_hh_id)
        return frozenset(
            vacancy_hh_id
            for vacancy_hh_id in has_unknown_result
            if len(resumes_by_vacancy[vacancy_hh_id]) > 1
        )

    @staticmethod
    def _attention_title(body: str) -> str | None:
        negated_interview = any(pattern.search(body) for pattern in _NEGATED_ATTENTION_PATTERNS)
        for pattern, title in _ATTENTION_PATTERNS:
            if not pattern.search(body):
                continue
            if title == _INTERVIEW_TITLE and negated_interview:
                continue
            return title
        return None

    @staticmethod
    def _booking_url(body: str) -> str | None:
        match = _HTTPS_URL.search(body)
        return match.group(0).rstrip(".,;:!?)»") if match is not None else None
