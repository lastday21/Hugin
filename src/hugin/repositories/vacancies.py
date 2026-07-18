from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import VacancyModel
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
        self._session.flush()
        return _to_record(model)

    def get_by_hh_id(self, hh_id: str) -> VacancyRecord | None:
        model = self._session.scalar(select(VacancyModel).where(VacancyModel.hh_id == hh_id))
        return _to_record(model) if model is not None else None
