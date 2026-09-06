from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import ApplicationModel, ApplicationOutcomeModel
from hugin.domain.search_outcomes import ApplicationOutcome, StaleOutcomeError
from hugin.domain.time import as_utc


def _record(model: ApplicationOutcomeModel) -> ApplicationOutcome:
    return ApplicationOutcome(
        revision=model.id,
        interview_at=model.interview_at,
        interview_evidence=model.interview_evidence,
        rejection_reason=model.rejection_reason,
        rejection_evidence=model.rejection_evidence,
        recorded_at=model.recorded_at,
    )


class ApplicationOutcomeService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def for_account(
        self, account_id: int, *, recorded_before: datetime | None = None
    ) -> dict[int, ApplicationOutcome]:
        query = (
            select(ApplicationOutcomeModel)
            .join(ApplicationModel)
            .where(ApplicationModel.account_id == account_id)
            .order_by(ApplicationOutcomeModel.application_id, ApplicationOutcomeModel.id.desc())
            .distinct(ApplicationOutcomeModel.application_id)
        )
        if recorded_before is not None:
            query = query.where(ApplicationOutcomeModel.recorded_at <= as_utc(recorded_before))
        return {model.application_id: _record(model) for model in self._session.scalars(query)}

    def save(
        self,
        account_id: int,
        application_id: int,
        *,
        revision: int,
        interview_at: datetime | None,
        interview_evidence: str,
        rejection_reason: str,
        rejection_evidence: str,
    ) -> ApplicationOutcome:
        application = self._session.scalar(
            select(ApplicationModel)
            .where(ApplicationModel.id == application_id, ApplicationModel.account_id == account_id)
            .with_for_update()
        )
        if application is None:
            raise LookupError("Отклик не найден")
        current = self._session.scalar(
            select(ApplicationOutcomeModel)
            .where(ApplicationOutcomeModel.application_id == application_id)
            .order_by(ApplicationOutcomeModel.id.desc())
            .limit(1)
        )
        if revision != (current.id if current is not None else 0):
            raise StaleOutcomeError("Результат уже изменён. Обновите сведения перед сохранением.")
        interview_evidence = interview_evidence.strip()
        rejection_reason = rejection_reason.strip()
        rejection_evidence = rejection_evidence.strip()
        if interview_at is not None and not interview_evidence:
            raise ValueError("Для даты встречи укажите подтверждение приглашения на собеседование.")
        if interview_at is not None and interview_at.utcoffset() is None:
            raise ValueError("У даты встречи должен быть указан часовой пояс.")
        if bool(rejection_reason) != bool(rejection_evidence):
            raise ValueError("Укажите причину отказа и объяснение работодателя вместе.")
        if (
            len(rejection_reason) > 500
            or max(len(interview_evidence), len(rejection_evidence)) > 2000
        ):
            raise ValueError("Сократите описание результата.")
        values = {
            "interview_at": as_utc(interview_at) if interview_at is not None else None,
            "interview_evidence": interview_evidence,
            "rejection_reason": rejection_reason,
            "rejection_evidence": rejection_evidence,
        }
        if current is not None and all(
            getattr(current, key) == value for key, value in values.items()
        ):
            return _record(current)
        model = ApplicationOutcomeModel(application_id=application.id, **values)
        self._session.add(model)
        self._session.flush()
        return _record(model)
