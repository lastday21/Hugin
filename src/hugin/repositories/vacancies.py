from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import DirectionVacancyModel, VacancyModel
from hugin.domain.time import as_utc
from hugin.domain.vacancies import VacancyData, VacancyRecord


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
        created_at=as_utc(model.created_at),
    )


class VacancyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, data: VacancyData) -> VacancyRecord:
        model = self._session.scalar(select(VacancyModel).where(VacancyModel.hh_id == data.hh_id))
        if model is None:
            model = VacancyModel(hh_id=data.hh_id)
            self._session.add(model)

        model.title = data.title
        model.source_url = data.source_url
        model.employer_name = data.employer_name
        model.published_at = data.published_at
        if data.details_fetched_at is not None:
            model.description = data.description
            model.experience = data.experience
            model.employment = data.employment
            model.work_format = data.work_format
            model.key_skills = list(data.key_skills)
            model.details_fetched_at = data.details_fetched_at
        self._session.flush()
        return _to_record(model)

    def get_by_hh_id(self, hh_id: str) -> VacancyRecord | None:
        model = self._session.scalar(select(VacancyModel).where(VacancyModel.hh_id == hh_id))
        return _to_record(model) if model is not None else None

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
