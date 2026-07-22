from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hugin.database.models import (
    DirectionVacancyModel,
    VacancyChangeModel,
    VacancyDiscoveryModel,
    VacancyModel,
)
from hugin.domain.time import as_utc
from hugin.domain.vacancies import (
    VacancyChangeRecord,
    VacancyData,
    VacancyDiscoveryRecord,
    VacancyRecord,
)


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

    def list_duplicate_candidates(self, vacancy: VacancyRecord) -> list[VacancyRecord]:
        if not vacancy.employer_name:
            return []
        models = self._session.scalars(
            select(VacancyModel)
            .where(
                VacancyModel.id < vacancy.id,
                VacancyModel.duplicate_of_id.is_(None),
                VacancyModel.details_fetched_at.is_not(None),
                func.lower(VacancyModel.employer_name) == vacancy.employer_name.casefold(),
            )
            .order_by(VacancyModel.created_at, VacancyModel.id)
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
        models = self._session.scalars(
            select(VacancyModel)
            .join(DirectionVacancyModel)
            .where(
                DirectionVacancyModel.direction_id == direction_id,
                VacancyModel.details_fetched_at.is_(None),
            )
            .order_by(VacancyModel.id)
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
