from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from hugin.domain.directions import DirectionVacancyRecord, VacancyState
from hugin.domain.vacancies import (
    VacancyChangeRecord,
    VacancyDiscoveryRecord,
    VacancyRecord,
)
from hugin.repositories.directions import AccountRepository, DirectionRepository
from hugin.repositories.vacancies import VacancyRepository


@dataclass(frozen=True, slots=True)
class VacancyReviewEntry:
    vacancy: VacancyRecord
    tracking: DirectionVacancyRecord
    discoveries: tuple[VacancyDiscoveryRecord, ...]
    changes: tuple[VacancyChangeRecord, ...]


class VacancyReviewService:
    def __init__(self, session: Session) -> None:
        self._accounts = AccountRepository(session)
        self._directions = DirectionRepository(session)
        self._vacancies = VacancyRepository(session)

    def list_rejected(
        self,
        *,
        account_id: int,
        direction_name: str,
        limit: int = 50,
        company: str | None = None,
        region: str | None = None,
        reason: str | None = None,
        sort: str = "newest",
    ) -> tuple[VacancyReviewEntry, ...]:
        if limit < 1:
            raise ValueError("Число вакансий должно быть положительным")
        direction_id = self._direction_id(account_id, direction_name)
        entries = [
            self._entry(link)
            for link in self._directions.list_tracked_vacancies(direction_id)
            if link.state in {VacancyState.FILTERED_OUT, VacancyState.CLOSED}
        ]
        if company:
            marker = company.casefold()
            entries = [
                entry
                for entry in entries
                if marker in (entry.vacancy.employer_name or "").casefold()
            ]
        if region:
            marker = region.casefold()
            entries = [
                entry for entry in entries if marker in (entry.vacancy.region or "").casefold()
            ]
        if reason:
            marker = reason.casefold()
            filtered: list[VacancyReviewEntry] = []
            for entry in entries:
                raw_reasons = entry.tracking.rules_details.get("reasons", [])
                reasons = raw_reasons if isinstance(raw_reasons, list) else []
                if any(marker in str(value).casefold() for value in reasons):
                    filtered.append(entry)
            entries = filtered
        if sort == "score":
            entries.sort(key=lambda entry: entry.tracking.rules_score or 0, reverse=True)
        elif sort == "company":
            entries.sort(key=lambda entry: (entry.vacancy.employer_name or "").casefold())
        elif sort == "newest":
            entries.sort(key=self._published_at, reverse=True)
        else:
            raise ValueError("Сортировка должна быть newest, score или company")
        return tuple(entries[:limit])

    def get_card(
        self,
        *,
        account_id: int,
        direction_name: str,
        hh_id: str,
    ) -> VacancyReviewEntry:
        direction_id = self._direction_id(account_id, direction_name)
        vacancy = self._vacancies.get_by_hh_id(hh_id)
        if vacancy is None:
            raise LookupError(f"Вакансия hh.ru {hh_id} не найдена")
        link = self._directions.get_tracked_vacancy(direction_id, vacancy.id)
        return self._entry(link)

    def restore(
        self,
        *,
        account_id: int,
        direction_name: str,
        hh_id: str,
    ) -> VacancyReviewEntry:
        direction_id = self._direction_id(account_id, direction_name)
        vacancy = self._vacancies.get_by_hh_id(hh_id)
        if vacancy is None:
            raise LookupError(f"Вакансия hh.ru {hh_id} не найдена")
        self._directions.restore_rejected(direction_id, vacancy.id)
        return self.get_card(account_id=account_id, direction_name=direction_name, hh_id=hh_id)

    def _direction_id(self, account_id: int, direction_name: str) -> int:
        self._accounts.get(account_id)
        direction = self._directions.get_by_account_and_name(account_id, direction_name)
        if direction is None:
            raise LookupError(f"Направление «{direction_name}» не найдено")
        return direction.id

    def _entry(self, link: DirectionVacancyRecord) -> VacancyReviewEntry:
        vacancy = self._vacancies.get(link.vacancy_id)
        return VacancyReviewEntry(
            vacancy=vacancy,
            tracking=link,
            discoveries=tuple(self._vacancies.list_discoveries(vacancy.id)),
            changes=tuple(self._vacancies.list_changes(vacancy.id)),
        )

    @staticmethod
    def _published_at(entry: VacancyReviewEntry) -> datetime:
        return entry.vacancy.published_at or entry.vacancy.created_at
