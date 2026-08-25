from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import case, exists, func, or_, select, update
from sqlalchemy.orm import Session

from hugin.database.models import (
    ApplicationEventModel,
    ApplicationModel,
    ApplicationTaskModel,
    DirectionVacancyModel,
    VacancyChangeModel,
    VacancyDiscoveryModel,
    VacancyModel,
)
from hugin.domain.applications import ApplicationEventType, ApplicationState
from hugin.domain.directions import VacancyState
from hugin.domain.tasks import TaskState
from hugin.domain.time import as_utc
from hugin.domain.vacancies import (
    VacancyAvailability,
    VacancyChangeRecord,
    VacancyData,
    VacancyDiscoveryRecord,
    VacancyRecord,
)
from hugin.repositories.tasks import FORM_PREFLIGHT_RUNNING


def _to_record(model: VacancyModel) -> VacancyRecord:
    return VacancyRecord(
        id=model.id,
        hh_id=model.hh_id,
        title=model.title,
        source_url=model.source_url,
        employer_name=model.employer_name,
        published_at=as_utc(model.published_at) if model.published_at is not None else None,
        description=model.description,
        experience=model.experience,
        employment=model.employment,
        work_format=model.work_format,
        key_skills=tuple(model.key_skills),
        details_fetched_at=(
            as_utc(model.details_fetched_at) if model.details_fetched_at is not None else None
        ),
        region=model.region,
        address=model.address,
        salary_from=model.salary_from,
        salary_to=model.salary_to,
        salary_currency=model.salary_currency,
        salary_gross=model.salary_gross,
        schedule=model.schedule,
        responsibilities=model.responsibilities,
        required_qualifications=model.required_qualifications,
        preferred_qualifications=model.preferred_qualifications,
        has_cover_letter=model.has_cover_letter,
        has_screening_form=model.has_screening_form,
        has_external_link=model.has_external_link,
        has_test_assignment=model.has_test_assignment,
        availability=model.availability,
        duplicate_of_id=model.duplicate_of_id,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _change_record(model: VacancyChangeModel) -> VacancyChangeRecord:
    return VacancyChangeRecord(
        id=model.id,
        vacancy_id=model.vacancy_id,
        event_type=model.event_type,
        changes=dict(model.changes),
        created_at=as_utc(model.created_at),
    )


def _discovery_record(model: VacancyDiscoveryModel) -> VacancyDiscoveryRecord:
    return VacancyDiscoveryRecord(
        id=model.id,
        vacancy_id=model.vacancy_id,
        direction_id=model.direction_id,
        search_query_id=model.search_query_id,
        query_text=model.query_text,
        region=model.region,
        discovered_at=as_utc(model.discovered_at),
    )


def _history_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


class VacancyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, data: VacancyData) -> VacancyRecord:
        model = self._session.scalar(select(VacancyModel).where(VacancyModel.hh_id == data.hh_id))
        created = model is None
        if model is None:
            model = VacancyModel(hh_id=data.hh_id)
            self._session.add(model)

        changes: dict[str, object] = {}
        self._set(model, "title", data.title, changes)
        self._set(model, "source_url", data.source_url, changes)
        if data.employer_name is not None or created:
            self._set(model, "employer_name", data.employer_name, changes)
        if data.published_at is not None or created:
            self._set(model, "published_at", data.published_at, changes)
        for field in (
            "region",
            "address",
            "salary_from",
            "salary_to",
            "salary_currency",
            "salary_gross",
        ):
            value = getattr(data, field)
            if value is not None or created:
                self._set(model, field, value, changes)
        if (
            created
            or data.details_fetched_at is not None
            or data.availability is not VacancyAvailability.ACTIVE
        ):
            self._set(model, "availability", data.availability, changes)
        if data.details_fetched_at is not None:
            for field in (
                "description",
                "experience",
                "employment",
                "work_format",
                "schedule",
                "responsibilities",
                "required_qualifications",
                "preferred_qualifications",
                "has_cover_letter",
                "has_screening_form",
                "has_external_link",
                "has_test_assignment",
            ):
                self._set(model, field, getattr(data, field), changes)
            self._set(model, "key_skills", list(data.key_skills), changes)
            model.details_fetched_at = data.details_fetched_at
        self._session.flush()
        if model.availability is not VacancyAvailability.ACTIVE:
            self._close_links_and_waiting_tasks(model.id, model.availability)
        if created or changes:
            self._session.add(
                VacancyChangeModel(
                    vacancy_id=model.id,
                    event_type="CREATED" if created else "UPDATED",
                    changes=changes,
                )
            )
            self._session.flush()
        return _to_record(model)

    @staticmethod
    def _set(
        model: VacancyModel,
        field: str,
        value: object,
        changes: dict[str, object],
    ) -> None:
        previous = getattr(model, field, None)
        if previous == value:
            return
        setattr(model, field, value)
        changes[field] = {
            "before": _history_value(previous),
            "after": _history_value(value),
        }

    def get_by_hh_id(self, hh_id: str) -> VacancyRecord | None:
        model = self._session.scalar(select(VacancyModel).where(VacancyModel.hh_id == hh_id))
        return _to_record(model) if model is not None else None

    def get(self, vacancy_id: int) -> VacancyRecord:
        model = self._session.get(VacancyModel, vacancy_id)
        if model is None:
            raise LookupError("vacancy was not found")
        return _to_record(model)

    def mark_unavailable(
        self,
        vacancy_id: int,
        availability: VacancyAvailability,
    ) -> VacancyRecord:
        if availability is VacancyAvailability.ACTIVE:
            raise ValueError("Для активной вакансии нужно использовать обычное обновление")
        model = self._session.get(VacancyModel, vacancy_id)
        if model is None:
            raise LookupError("vacancy was not found")
        changes: dict[str, object] = {}
        self._set(model, "availability", availability, changes)
        self._close_links_and_waiting_tasks(vacancy_id, availability)
        if changes:
            self._session.add(
                VacancyChangeModel(
                    vacancy_id=vacancy_id,
                    event_type="AVAILABILITY_CHANGED",
                    changes=changes,
                )
            )
        self._session.flush()
        return _to_record(model)

    def _close_links_and_waiting_tasks(
        self,
        vacancy_id: int,
        availability: VacancyAvailability,
    ) -> None:
        self._session.execute(
            update(DirectionVacancyModel)
            .where(DirectionVacancyModel.vacancy_id == vacancy_id)
            .values(state=VacancyState.CLOSED)
        )
        waiting_states = (
            TaskState.PENDING,
            TaskState.RETRY_SCHEDULED,
            TaskState.REVIEW_REQUIRED,
            TaskState.INPUT_REQUIRED,
        )
        waiting_or_preflight = or_(
            ApplicationTaskModel.state.in_(waiting_states),
            (
                (ApplicationTaskModel.state == TaskState.RUNNING)
                & (ApplicationTaskModel.last_error_code == FORM_PREFLIGHT_RUNNING)
            ),
        )
        waiting_applications = tuple(
            self._session.scalars(
                select(ApplicationModel)
                .join(
                    ApplicationTaskModel,
                    ApplicationTaskModel.application_id == ApplicationModel.id,
                )
                .where(
                    ApplicationModel.vacancy_id == vacancy_id,
                    ApplicationModel.state == ApplicationState.APPLYING,
                    waiting_or_preflight,
                )
            )
        )
        for application in waiting_applications:
            application.state = ApplicationState.CLOSED
            application.events.append(
                ApplicationEventModel(
                    event_type=ApplicationEventType.STATE_CHANGED,
                    payload={
                        "previous_state": ApplicationState.APPLYING.value,
                        "state": ApplicationState.CLOSED.value,
                        "reason": f"VACANCY_{availability.value}",
                    },
                )
            )
        application_ids = tuple(application.id for application in waiting_applications)
        if not application_ids:
            return
        waiting_tasks = self._session.scalars(
            select(ApplicationTaskModel).where(
                ApplicationTaskModel.application_id.in_(application_ids),
                waiting_or_preflight,
            )
        )
        for task in waiting_tasks:
            task.state = TaskState.SKIPPED
            task.last_error_code = f"VACANCY_{availability.value}"

    def list_duplicate_candidates(self, vacancy: VacancyRecord) -> list[VacancyRecord]:
        if not vacancy.employer_name:
            return []
        models = self._session.scalars(
            select(VacancyModel)
            .where(
                VacancyModel.id < vacancy.id,
                VacancyModel.duplicate_of_id.is_(None),
                VacancyModel.details_fetched_at.is_not(None),
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                func.lower(VacancyModel.employer_name) == vacancy.employer_name.casefold(),
            )
            .order_by(
                VacancyModel.published_at.desc().nulls_last(),
                VacancyModel.created_at.desc(),
                VacancyModel.id.desc(),
            )
            .limit(100)
        )
        return [_to_record(model) for model in models]

    def mark_duplicate(
        self,
        vacancy_id: int,
        canonical_id: int,
        similarity: float,
    ) -> VacancyRecord:
        if vacancy_id == canonical_id:
            raise ValueError("vacancy cannot be its own duplicate")
        model = self._session.get(VacancyModel, vacancy_id)
        canonical = self._session.get(VacancyModel, canonical_id)
        if model is None or canonical is None:
            raise LookupError("vacancy was not found")
        actual_canonical_id = canonical.duplicate_of_id or canonical.id
        if model.duplicate_of_id != actual_canonical_id:
            previous = model.duplicate_of_id
            model.duplicate_of_id = actual_canonical_id
            self._session.flush()
            self._session.add(
                VacancyChangeModel(
                    vacancy_id=model.id,
                    event_type="DUPLICATE_LINKED",
                    changes={
                        "duplicate_of_id": {
                            "before": previous,
                            "after": actual_canonical_id,
                        },
                        "similarity": round(similarity, 4),
                    },
                )
            )
            self._session.flush()
        return _to_record(model)

    def duplicate_family_ids(self, vacancy_id: int) -> tuple[int, ...]:
        model = self._session.get(VacancyModel, vacancy_id)
        if model is None:
            raise LookupError("vacancy was not found")
        canonical_id = model.duplicate_of_id or model.id
        return tuple(
            self._session.scalars(
                select(VacancyModel.id)
                .where(
                    or_(
                        VacancyModel.id == canonical_id,
                        VacancyModel.duplicate_of_id == canonical_id,
                    )
                )
                .order_by(VacancyModel.id)
            )
        )

    def duplicate_family_has_sent_or_live_application(
        self,
        account_id: int,
        vacancy_id: int,
    ) -> bool:
        family_ids = self.duplicate_family_ids(vacancy_id)
        live_task_states = (
            TaskState.PENDING,
            TaskState.RUNNING,
            TaskState.RETRY_SCHEDULED,
            TaskState.REVIEW_REQUIRED,
            TaskState.INPUT_REQUIRED,
            TaskState.UNKNOWN_RESULT,
        )
        sent_states = (
            ApplicationState.APPLIED,
            ApplicationState.VIEWED,
            ApplicationState.INVITED,
            ApplicationState.REJECTED,
        )
        return bool(
            self._session.scalar(
                select(ApplicationModel.id)
                .outerjoin(
                    ApplicationTaskModel,
                    ApplicationTaskModel.application_id == ApplicationModel.id,
                )
                .where(
                    ApplicationModel.account_id == account_id,
                    ApplicationModel.vacancy_id.in_(family_ids),
                    or_(
                        ApplicationModel.state.in_(sent_states),
                        ApplicationTaskModel.state.in_(live_task_states),
                    ),
                )
                .limit(1)
            )
        )

    def promote_duplicate(self, vacancy_id: int) -> VacancyRecord:
        model = self._session.get(VacancyModel, vacancy_id)
        if model is None:
            raise LookupError("vacancy was not found")
        if model.duplicate_of_id is None:
            return _to_record(model)
        previous_canonical_id = model.duplicate_of_id
        model.duplicate_of_id = None
        self._session.flush()
        family = tuple(
            self._session.scalars(
                select(VacancyModel).where(
                    or_(
                        VacancyModel.id == previous_canonical_id,
                        VacancyModel.duplicate_of_id == previous_canonical_id,
                    ),
                    VacancyModel.id != vacancy_id,
                )
            )
        )
        for member in family:
            member.duplicate_of_id = vacancy_id
        self._session.add(
            VacancyChangeModel(
                vacancy_id=vacancy_id,
                event_type="DUPLICATE_PROMOTED",
                changes={
                    "duplicate_of_id": {
                        "before": previous_canonical_id,
                        "after": None,
                    },
                    "relinked_vacancy_ids": [member.id for member in family],
                },
            )
        )
        self._session.flush()
        return _to_record(model)

    def list_changes(self, vacancy_id: int) -> list[VacancyChangeRecord]:
        models = self._session.scalars(
            select(VacancyChangeModel)
            .where(VacancyChangeModel.vacancy_id == vacancy_id)
            .order_by(VacancyChangeModel.created_at, VacancyChangeModel.id)
        )
        return [_change_record(model) for model in models]

    def list_discoveries(self, vacancy_id: int) -> list[VacancyDiscoveryRecord]:
        models = self._session.scalars(
            select(VacancyDiscoveryModel)
            .where(VacancyDiscoveryModel.vacancy_id == vacancy_id)
            .order_by(VacancyDiscoveryModel.discovered_at, VacancyDiscoveryModel.id)
        )
        return [_discovery_record(model) for model in models]

    def list_pending_for_direction(
        self,
        direction_id: int,
        *,
        limit: int,
    ) -> list[VacancyRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        refresh_before = datetime.now(UTC) - timedelta(hours=24)
        ready_task_exists = exists(
            select(ApplicationTaskModel.id)
            .join(
                ApplicationModel,
                ApplicationModel.id == ApplicationTaskModel.application_id,
            )
            .where(
                ApplicationModel.vacancy_id == VacancyModel.id,
                ApplicationModel.direction_id == direction_id,
                ApplicationModel.state == ApplicationState.APPLYING,
                ApplicationTaskModel.state.in_((TaskState.PENDING, TaskState.RETRY_SCHEDULED)),
            )
        )
        terminal_application_exists = exists(
            select(ApplicationModel.id)
            .outerjoin(
                ApplicationTaskModel,
                ApplicationTaskModel.application_id == ApplicationModel.id,
            )
            .where(
                ApplicationModel.vacancy_id == VacancyModel.id,
                ApplicationModel.direction_id == direction_id,
                or_(
                    ApplicationModel.state != ApplicationState.APPLYING,
                    ApplicationTaskModel.state.in_((TaskState.COMPLETED, TaskState.SKIPPED)),
                ),
            )
        )
        ready_priority = case((ready_task_exists, 0), else_=1)
        never_fetched_priority = case(
            (VacancyModel.details_fetched_at.is_(None), 0),
            else_=1,
        )
        never_fetched_recency = case(
            (
                VacancyModel.details_fetched_at.is_(None),
                VacancyModel.created_at,
            ),
        )
        due_at = case(
            (ready_task_exists, VacancyModel.details_fetched_at),
            else_=func.coalesce(
                VacancyModel.details_fetched_at,
                VacancyModel.created_at,
            ),
        )
        models = self._session.scalars(
            select(VacancyModel)
            .join(DirectionVacancyModel)
            .where(
                DirectionVacancyModel.direction_id == direction_id,
                VacancyModel.availability == VacancyAvailability.ACTIVE,
                or_(ready_task_exists, ~terminal_application_exists),
                (
                    VacancyModel.details_fetched_at.is_(None)
                    | (VacancyModel.details_fetched_at < refresh_before)
                ),
            )
            .order_by(
                ready_priority,
                never_fetched_priority,
                never_fetched_recency.desc().nulls_last(),
                due_at.asc().nulls_first(),
                VacancyModel.created_at,
                VacancyModel.id,
            )
            .limit(limit)
        )
        return [_to_record(model) for model in models]

    def list_detailed_for_direction(self, direction_id: int) -> list[VacancyRecord]:
        models = self._session.scalars(
            select(VacancyModel)
            .join(DirectionVacancyModel)
            .where(
                DirectionVacancyModel.direction_id == direction_id,
                VacancyModel.details_fetched_at.is_not(None),
            )
            .order_by(VacancyModel.id)
        )
        return [_to_record(model) for model in models]
