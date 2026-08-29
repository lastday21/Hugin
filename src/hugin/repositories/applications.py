from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    CareerDirectionModel,
    CoverLetterModel,
    DirectionVacancyModel,
    ResumeModel,
)
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
from hugin.domain.tasks import TaskState
from hugin.domain.time import as_utc


def _application_record(model: ApplicationModel) -> ApplicationRecord:
    return ApplicationRecord(
        id=model.id,
        account_id=model.account_id,
        vacancy_id=model.vacancy_id,
        resume_id=model.resume_id,
        direction_id=model.direction_id,
        state=model.state,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _event_record(model: ApplicationEventModel) -> ApplicationEventRecord:
    return ApplicationEventRecord(
        id=model.id,
        application_id=model.application_id,
        event_type=model.event_type,
        payload=deepcopy(model.payload),
        created_at=as_utc(model.created_at),
    )


class ApplicationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_apply_intent(
        self,
        account_id: int,
        vacancy_id: int,
        resume_id: int,
        direction_id: int | None = None,
    ) -> ApplicationRecord:
        resume_account_id = self._session.scalar(
            select(ResumeModel.account_id).where(ResumeModel.id == resume_id)
        )
        if resume_account_id != account_id:
            raise ValueError("resume must belong to the application account")
        if direction_id is not None:
            direction_account_id = self._session.scalar(
                select(CareerDirectionModel.account_id).where(
                    CareerDirectionModel.id == direction_id
                )
            )
            if direction_account_id != account_id:
                raise ValueError("direction must belong to the application account")

        existing_id = self._session.scalar(
            select(ApplicationModel.id).where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.vacancy_id == vacancy_id,
                ApplicationModel.resume_id == resume_id,
            )
        )
        if existing_id is not None:
            raise DuplicateApplicationError(account_id, vacancy_id, resume_id)

        application = ApplicationModel(
            account_id=account_id,
            vacancy_id=vacancy_id,
            resume_id=resume_id,
            direction_id=direction_id,
            state=ApplicationState.APPLYING,
        )
        application.events.append(
            ApplicationEventModel(
                event_type=ApplicationEventType.APPLY_INTENT,
                payload={
                    "account_id": account_id,
                    "resume_id": resume_id,
                    "direction_id": direction_id,
                },
            )
        )
        self._session.add(application)
        self._session.flush()
        return _application_record(application)

    def get_by_key(
        self,
        account_id: int,
        vacancy_id: int,
        resume_id: int,
    ) -> ApplicationRecord | None:
        model = self._session.scalar(
            select(ApplicationModel).where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.vacancy_id == vacancy_id,
                ApplicationModel.resume_id == resume_id,
            )
        )
        return _application_record(model) if model is not None else None

    def get_for_account_vacancy(
        self,
        account_id: int,
        vacancy_id: int,
    ) -> ApplicationRecord | None:
        model = self._session.scalar(
            select(ApplicationModel)
            .where(
                ApplicationModel.account_id == account_id,
                ApplicationModel.vacancy_id == vacancy_id,
            )
            .order_by(ApplicationModel.id)
            .limit(1)
        )
        return _application_record(model) if model is not None else None

    def get(self, application_id: int) -> ApplicationRecord:
        model = self._session.get(ApplicationModel, application_id)
        if model is None:
            raise ApplicationNotFoundError(application_id)
        return _application_record(model)

    def reassign_direction(
        self,
        application_id: int,
        direction_id: int,
    ) -> ApplicationRecord:
        model = self._session.get(ApplicationModel, application_id)
        if model is None:
            raise ApplicationNotFoundError(application_id)
        if model.state is not ApplicationState.APPLYING:
            raise ValueError("Можно перенести только неотправленный отклик")
        direction_account_id = self._session.scalar(
            select(CareerDirectionModel.account_id).where(CareerDirectionModel.id == direction_id)
        )
        if direction_account_id != model.account_id:
            raise ValueError("Направление должно принадлежать аккаунту отклика")
        model.direction_id = direction_id
        self._session.flush()
        return _application_record(model)

    def count_applied_since(self, account_id: int, since: datetime) -> int:
        return (
            self._session.scalar(
                select(func.count(func.distinct(ApplicationEventModel.application_id)))
                .select_from(ApplicationEventModel)
                .join(ApplicationModel)
                .outerjoin(
                    ApplicationTaskModel,
                    ApplicationTaskModel.application_id == ApplicationModel.id,
                )
                .where(
                    ApplicationModel.account_id == account_id,
                    ApplicationEventModel.created_at >= since,
                    or_(
                        and_(
                            ApplicationEventModel.event_type == ApplicationEventType.APPLIED,
                            ApplicationEventModel.payload["hh_status"].as_string()
                            == ApplicationState.APPLIED.value,
                            func.coalesce(
                                ApplicationEventModel.payload["source"].as_string(),
                                "",
                            )
                            != "hh.ru",
                        ),
                        and_(
                            ApplicationEventModel.event_type == ApplicationEventType.UNKNOWN_RESULT,
                            ApplicationTaskModel.state == TaskState.UNKNOWN_RESULT,
                        ),
                    ),
                )
            )
            or 0
        )

    def list_by_vacancy_id(self, vacancy_id: int) -> list[ApplicationRecord]:
        models = self._session.scalars(
            select(ApplicationModel)
            .where(ApplicationModel.vacancy_id == vacancy_id)
            .order_by(ApplicationModel.id)
        )
        return [_application_record(model) for model in models]

    def list_events(self, application_id: int) -> list[ApplicationEventRecord]:
        events = self._session.scalars(
            select(ApplicationEventModel)
            .where(ApplicationEventModel.application_id == application_id)
            .order_by(ApplicationEventModel.id)
        )
        return [_event_record(event) for event in events]

    def append_event(
        self,
        application_id: int,
        event_type: ApplicationEventType,
        payload: EventPayload | None = None,
    ) -> ApplicationEventRecord:
        application = self._session.get(ApplicationModel, application_id)
        if application is None:
            raise ApplicationNotFoundError(application_id)
        event_payload = dict(payload or {})
        if event_type is ApplicationEventType.APPLIED:
            event_payload = self._applied_event_payload(application, event_payload)
        event = ApplicationEventModel(
            application_id=application_id,
            event_type=event_type,
            payload=event_payload,
        )
        self._session.add(event)
        self._session.flush()
        return _event_record(event)

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
        if event_type is ApplicationEventType.APPLIED:
            event_payload = self._applied_event_payload(application, event_payload)
        application.events.append(
            ApplicationEventModel(event_type=event_type, payload=event_payload)
        )
        self._session.flush()
        return _application_record(application)

    def _applied_event_payload(
        self,
        application: ApplicationModel,
        payload: EventPayload,
    ) -> EventPayload:
        result = dict(payload)
        result.pop("cover_letter_id", None)
        result.pop("cover_letter_instruction_version", None)
        result.pop("selection_snapshot", None)

        source = payload.get("source")
        snapshot_required = source in {"hugin_send", "hugin_reconciliation"}
        attempt_snapshot = None
        if source == "hugin_send":
            attempt_snapshot = self._validated_selection_snapshot(payload.get("selection_snapshot"))
        elif source == "hugin_reconciliation":
            task_id = payload.get("task_id")
            if isinstance(task_id, int) and not isinstance(task_id, bool):
                attempt_snapshot = self._unknown_result_snapshot(application.id, task_id)

        if snapshot_required and attempt_snapshot is None:
            result.update(
                {
                    "category": None,
                    "fit_score": None,
                    "rules_version": None,
                    "rules_details": {},
                    "direction_id": application.direction_id,
                    "resume_id": application.resume_id,
                    "snapshot_missing": True,
                }
            )
        elif attempt_snapshot is not None:
            result.update(
                {
                    key: deepcopy(attempt_snapshot[key])
                    for key in (
                        "category",
                        "fit_score",
                        "rules_version",
                        "rules_details",
                        "direction_id",
                        "resume_id",
                    )
                }
            )
            result["snapshot_missing"] = False
        else:
            tracked = None
            if application.direction_id is not None:
                tracked = self._session.get(
                    DirectionVacancyModel,
                    (application.direction_id, application.vacancy_id),
                )
            rules_details = deepcopy(tracked.rules_details) if tracked is not None else {}
            category = rules_details.get("category")
            fit_score = None
            if tracked is not None:
                fit_score = (
                    tracked.fit_score if tracked.fit_score is not None else tracked.rules_score
                )
            result.update(
                {
                    "category": category if isinstance(category, str) else None,
                    "fit_score": fit_score,
                    "rules_version": tracked.rules_version if tracked is not None else None,
                    "rules_details": rules_details,
                    "direction_id": application.direction_id,
                    "resume_id": application.resume_id,
                }
            )

        if attempt_snapshot is not None:
            requested_letter_id = attempt_snapshot.get("cover_letter_id")
            instruction_version = attempt_snapshot.get("cover_letter_instruction_version")
            if isinstance(requested_letter_id, int) and not isinstance(requested_letter_id, bool):
                owned_letter_id = self._session.scalar(
                    select(CoverLetterModel.id)
                    .where(
                        CoverLetterModel.id == requested_letter_id,
                        CoverLetterModel.application_id == application.id,
                    )
                    .limit(1)
                )
                if owned_letter_id is not None:
                    result["cover_letter_id"] = owned_letter_id
                    if isinstance(instruction_version, str):
                        result["cover_letter_instruction_version"] = instruction_version
        elif not snapshot_required:
            requested_letter_id = payload.get("cover_letter_id")
            letter = None
            if isinstance(requested_letter_id, int) and not isinstance(requested_letter_id, bool):
                letter = self._session.scalar(
                    select(CoverLetterModel)
                    .where(
                        CoverLetterModel.id == requested_letter_id,
                        CoverLetterModel.application_id == application.id,
                    )
                    .limit(1)
                )
            if letter is not None:
                result["cover_letter_id"] = letter.id
                result["cover_letter_instruction_version"] = letter.instruction_version
        return result

    @staticmethod
    def _validated_selection_snapshot(value: object) -> EventPayload | None:
        if not isinstance(value, dict):
            return None
        snapshot = value
        required = {
            "category",
            "fit_score",
            "rules_version",
            "rules_details",
            "direction_id",
            "resume_id",
        }
        if not required.issubset(snapshot) or not isinstance(snapshot["rules_details"], dict):
            return None
        category = snapshot["category"]
        fit_score = snapshot["fit_score"]
        rules_version = snapshot["rules_version"]
        direction_id = snapshot["direction_id"]
        resume_id = snapshot["resume_id"]
        cover_letter_id = snapshot.get("cover_letter_id")
        instruction_version = snapshot.get("cover_letter_instruction_version")
        if category is not None and not isinstance(category, str):
            return None
        if fit_score is not None and (
            not isinstance(fit_score, (int, float)) or isinstance(fit_score, bool)
        ):
            return None
        if rules_version is not None and not isinstance(rules_version, str):
            return None
        if direction_id is not None and (
            not isinstance(direction_id, int) or isinstance(direction_id, bool)
        ):
            return None
        if not isinstance(resume_id, int) or isinstance(resume_id, bool):
            return None
        if cover_letter_id is not None and (
            not isinstance(cover_letter_id, int) or isinstance(cover_letter_id, bool)
        ):
            return None
        if instruction_version is not None and not isinstance(instruction_version, str):
            return None
        return deepcopy(snapshot)

    def _unknown_result_snapshot(
        self,
        application_id: int,
        task_id: int,
    ) -> EventPayload | None:
        events = self._session.scalars(
            select(ApplicationEventModel)
            .where(
                ApplicationEventModel.application_id == application_id,
                ApplicationEventModel.event_type == ApplicationEventType.UNKNOWN_RESULT,
            )
            .order_by(ApplicationEventModel.id.desc())
        )
        snapshot = None
        for event in events:
            event_task_id = event.payload.get("task_id")
            if (
                isinstance(event_task_id, int)
                and not isinstance(event_task_id, bool)
                and event_task_id == task_id
            ):
                snapshot = event.payload.get("selection_snapshot")
                break
        return self._validated_selection_snapshot(snapshot)
