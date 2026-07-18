from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import ApplicationEventModel, ApplicationModel
from hugin.domain.applications import (
    ApplicationEventRecord,
    ApplicationEventType,
    ApplicationNotFoundError,
    ApplicationRecord,
    ApplicationState,
    DuplicateApplicationError,
    EventPayload,
)
from hugin.domain.state_machines import ensure_application_transition
from hugin.domain.time import as_utc


def _application_record(model: ApplicationModel) -> ApplicationRecord:
    return ApplicationRecord(
        id=model.id,
        vacancy_id=model.vacancy_id,
        resume_hh_id=model.resume_hh_id,
        state=model.state,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _event_record(model: ApplicationEventModel) -> ApplicationEventRecord:
    return ApplicationEventRecord(
        id=model.id,
        application_id=model.application_id,
        event_type=model.event_type,
        payload=dict(model.payload),
        created_at=as_utc(model.created_at),
    )


class ApplicationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_apply_intent(self, vacancy_id: int, resume_hh_id: str) -> ApplicationRecord:
        existing_id = self._session.scalar(
            select(ApplicationModel.id).where(ApplicationModel.vacancy_id == vacancy_id)
        )
        if existing_id is not None:
            raise DuplicateApplicationError(vacancy_id)

        application = ApplicationModel(
            vacancy_id=vacancy_id,
            resume_hh_id=resume_hh_id,
            state=ApplicationState.APPLYING,
        )
        application.events.append(
            ApplicationEventModel(
                event_type=ApplicationEventType.APPLY_INTENT,
                payload={"resume_hh_id": resume_hh_id},
            )
        )
        self._session.add(application)
        self._session.flush()
        return _application_record(application)

    def get_by_vacancy_id(self, vacancy_id: int) -> ApplicationRecord | None:
        model = self._session.scalar(
            select(ApplicationModel).where(ApplicationModel.vacancy_id == vacancy_id)
        )
        return _application_record(model) if model is not None else None

    def list_events(self, application_id: int) -> list[ApplicationEventRecord]:
        events = self._session.scalars(
            select(ApplicationEventModel)
            .where(ApplicationEventModel.application_id == application_id)
            .order_by(ApplicationEventModel.id)
        )
        return [_event_record(event) for event in events]

    def transition_state(
        self,
        application_id: int,
        target: ApplicationState,
        payload: EventPayload | None = None,
    ) -> ApplicationRecord:
        application = self._session.get(ApplicationModel, application_id)
        if application is None:
            raise ApplicationNotFoundError(application_id)

        previous = application.state
        ensure_application_transition(previous, target)
        application.state = target
        event_type = (
            ApplicationEventType.APPLIED
            if target is ApplicationState.APPLIED
            else ApplicationEventType.STATE_CHANGED
        )
        event_payload: EventPayload = dict(payload or {})
        event_payload.update(
            {
                "previous_state": previous.value,
                "state": target.value,
            }
        )
        application.events.append(
            ApplicationEventModel(event_type=event_type, payload=event_payload)
        )
        self._session.flush()
        return _application_record(application)
