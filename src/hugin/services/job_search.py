from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hugin.domain.directions import DirectionRecord, ResumeRecord, SearchQueryRecord
from hugin.domain.vacancies import VacancyData, VacancyRecord
from hugin.repositories.directions import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
)
from hugin.repositories.vacancies import VacancyRepository


@dataclass(frozen=True, slots=True)
class JobSearchSyncResult:
    direction: DirectionRecord
    query: SearchQueryRecord
    resume: ResumeRecord
    vacancies: tuple[VacancyRecord, ...]


class JobSearchSyncService:
    def __init__(self, session: Session) -> None:
        self._accounts = AccountRepository(session)
        self._resumes = ResumeRepository(session)
        self._directions = DirectionRepository(session)
        self._vacancies = VacancyRepository(session)

    def synchronize(
        self,
        *,
        account_external_id: str,
        direction_name: str,
        resume_title: str,
        query: str,
        area: str,
        filters: dict[str, object],
        vacancies: tuple[VacancyData, ...],
    ) -> JobSearchSyncResult:
        account = self._accounts.get_by_external_id(account_external_id)
        if account is None:
            raise LookupError("Аккаунт hh.ru сначала нужно загрузить в базу")

        resume = self._resumes.get_active_by_title(account.id, resume_title)
        direction = self._directions.upsert(account.id, direction_name)
        search_query = self._directions.upsert_query(
            direction.id,
            query,
            area=area,
            filters=filters,
        )
        self._directions.attach_resume(direction.id, resume.id)

        stored: list[VacancyRecord] = []
        for vacancy in vacancies:
            record = self._vacancies.upsert(vacancy)
            self._directions.track_vacancy(direction.id, record.id)
            stored.append(record)

        return JobSearchSyncResult(
            direction=direction,
            query=search_query,
            resume=resume,
            vacancies=tuple(stored),
        )
