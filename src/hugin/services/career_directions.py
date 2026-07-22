from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hugin.database.models import CandidateProfileModel, VerifiedFactModel
from hugin.domain.content import ConfirmationState
from hugin.domain.directions import (
    DirectionRecord,
    EmploymentForm,
    ResumeRecord,
    SearchQueryRecord,
    SearchRegion,
    WorkFormat,
)
from hugin.repositories.directions import (
    AccountRepository,
    DirectionRepository,
    ResumeRepository,
)

RUSSIA_REGION = SearchRegion("113", "Россия")

COMMON_REGIONS = {
    "москва": SearchRegion("1", "Москва"),
    "санкт-петербург": SearchRegion("2", "Санкт-Петербург"),
    "спб": SearchRegion("2", "Санкт-Петербург"),
    "екатеринбург": SearchRegion("3", "Екатеринбург"),
    "новосибирск": SearchRegion("4", "Новосибирск"),
    "воронеж": SearchRegion("26", "Воронеж"),
    "краснодар": SearchRegion("53", "Краснодар"),
    "красноярск": SearchRegion("54", "Красноярск"),
    "нижний новгород": SearchRegion("66", "Нижний Новгород"),
    "омск": SearchRegion("68", "Омск"),
    "пермь": SearchRegion("72", "Пермь"),
    "ростов-на-дону": SearchRegion("76", "Ростов-на-Дону"),
    "самара": SearchRegion("78", "Самара"),
    "казань": SearchRegion("88", "Казань"),
    "тюмень": SearchRegion("95", "Тюмень"),
    "уфа": SearchRegion("99", "Уфа"),
    "челябинск": SearchRegion("104", "Челябинск"),
}


@dataclass(frozen=True, slots=True)
class DirectionSearchSettings:
    direction: DirectionRecord
    resume: ResumeRecord
    queries: tuple[SearchQueryRecord, ...]
    work_formats: tuple[WorkFormat, ...]
    employment_forms: tuple[EmploymentForm, ...]
    minimum_salary: int | None
    desired_salary: int | None
    salary_currency: str
    remote_all_russia: bool
    skills_from_resume: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VacancySearchTask:
    search_query_id: int | None
    query: str
    area: str
    region_name: str
    filters: dict[str, object]


class CareerDirectionService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._accounts = AccountRepository(session)
        self._resumes = ResumeRepository(session)
        self._directions = DirectionRepository(session)

    def configure(
        self,
        *,
        account_id: int,
        direction_name: str,
        queries: tuple[str, ...] | None,
        regions: tuple[SearchRegion, ...],
        work_formats: tuple[WorkFormat, ...] | None = None,
        employment_forms: tuple[EmploymentForm, ...] | None = None,
        minimum_salary: int | None = None,
        desired_salary: int | None = None,
        remote_all_russia: bool | None = None,
        schedule_minutes: int = 120,
    ) -> DirectionSearchSettings:
        self._accounts.get(account_id)
        resume = self._resumes.get_profile_active(account_id)
        facts = self._confirmed_facts(account_id, resume.id)

        normalized_queries = self._queries(queries, facts, resume.title)
        normalized_regions = self._unique_regions(regions) or (RUSSIA_REGION,)
        normalized_formats = self._work_formats(work_formats, facts)
        normalized_employment = self._employment_forms(employment_forms, facts)
        fact_minimum, fact_desired = self._salary_from_facts(facts)
        actual_minimum = minimum_salary if minimum_salary is not None else fact_minimum
        actual_desired = desired_salary if desired_salary is not None else fact_desired
        self._validate_salary(actual_minimum, actual_desired)

        remote_included = WorkFormat.REMOTE in normalized_formats
        actual_remote_all_russia = (
            remote_included if remote_all_russia is None else remote_all_russia
        )
        if actual_remote_all_russia and not remote_included:
            raise ValueError(
                "Поиск удалённых вакансий по всей России можно включить "
                "только для удалённого формата"
            )
        existing = self._directions.get_by_account_and_name(account_id, direction_name.strip())
        scoring_config = dict(existing.scoring_config) if existing is not None else {}
        scoring_config["search_settings"] = {
            "minimum_salary": actual_minimum,
            "desired_salary": actual_desired,
            "salary_currency": "RUB",
            "employment_forms": [value.value for value in normalized_employment],
            "remote_all_russia": actual_remote_all_russia,
            "skills_source": "active_resume_confirmed_facts",
        }
        direction = self._directions.upsert(
            account_id,
            direction_name.strip(),
            scoring_config=scoring_config,
        )
        self._directions.attach_resume(direction.id, resume.id)

        filters: dict[str, object] = {"order_by": "publication_time"}
        if normalized_employment:
            filters["employment_form"] = [value.value for value in normalized_employment]
        if actual_minimum is not None:
            filters.update(
                {
                    "salary": actual_minimum,
                    "currency": "RUR",
                    "only_with_salary": True,
                }
            )

        stored_queries = tuple(
            self._directions.upsert_query(
                direction.id,
                query,
                filters=filters,
                regions=normalized_regions,
                work_formats=normalized_formats,
                schedule_minutes=schedule_minutes,
            )
            for query in normalized_queries
        )
        self._directions.disable_other_configured_queries(
            direction.id,
            {query.id for query in stored_queries},
        )
        refreshed_direction = self._directions.get_by_account_and_name(
            account_id, direction_name.strip()
        )
        if refreshed_direction is None:
            raise RuntimeError("Направление не сохранилось")
        return self._settings(refreshed_direction, resume, stored_queries, facts)

    def get(self, account_id: int, direction_name: str) -> DirectionSearchSettings:
        self._accounts.get(account_id)
        direction = self._directions.get_by_account_and_name(account_id, direction_name)
        if direction is None or not direction.is_active:
            raise LookupError(f"Направление «{direction_name}» не найдено")
        resume = self._resumes.get_profile_active(account_id)
        queries = tuple(self._directions.list_configured_queries(direction.id))
        if not queries:
            raise LookupError(f"У направления «{direction_name}» нет настроенных запросов")
        facts = self._confirmed_facts(account_id, resume.id)
        return self._settings(direction, resume, queries, facts)

    def build_search_tasks(
        self,
        account_id: int,
        direction_name: str,
    ) -> tuple[VacancySearchTask, ...]:
        settings = self.get(account_id, direction_name)
        tasks: list[VacancySearchTask] = []
        for query in settings.queries:
            formats = set(query.work_formats)
            local_formats = set(formats)
            if settings.remote_all_russia:
                local_formats.discard(WorkFormat.REMOTE)

            if query.regions and (local_formats or not formats):
                for region in query.regions:
                    filters = dict(query.filters)
                    if local_formats:
                        filters["work_format"] = sorted(value.value for value in local_formats)
                    tasks.append(
                        VacancySearchTask(
                            query.id,
                            query.query,
                            region.area,
                            region.name,
                            filters,
                        )
                    )

            if settings.remote_all_russia:
                filters = dict(query.filters)
                filters["work_format"] = [WorkFormat.REMOTE.value]
                tasks.append(
                    VacancySearchTask(
                        query.id,
                        query.query,
                        RUSSIA_REGION.area,
                        "Россия — удалённо",
                        filters,
                    )
                )
        return tuple(tasks)

    def _settings(
        self,
        direction: DirectionRecord,
        resume: ResumeRecord,
        queries: tuple[SearchQueryRecord, ...],
        facts: dict[str, tuple[str, ...]],
    ) -> DirectionSearchSettings:
        raw = direction.scoring_config.get("search_settings", {})
        search = raw if isinstance(raw, dict) else {}
        first_query = queries[0]
        return DirectionSearchSettings(
            direction=direction,
            resume=resume,
            queries=queries,
            work_formats=first_query.work_formats,
            employment_forms=tuple(
                EmploymentForm(value)
                for value in search.get("employment_forms", [])
                if isinstance(value, str)
            ),
            minimum_salary=self._optional_int(search.get("minimum_salary")),
            desired_salary=self._optional_int(search.get("desired_salary")),
            salary_currency=str(search.get("salary_currency", "RUB")),
            remote_all_russia=search.get("remote_all_russia") is True,
            skills_from_resume=facts.get("skills", ()),
        )

    def _confirmed_facts(
        self,
        account_id: int,
        resume_id: int,
    ) -> dict[str, tuple[str, ...]]:
        profile = self._session.scalar(
            select(CandidateProfileModel).where(CandidateProfileModel.account_id == account_id)
        )
        if profile is None:
            raise LookupError("Профиль кандидата не найден")
        models = self._session.scalars(
            select(VerifiedFactModel)
            .where(
                VerifiedFactModel.profile_id == profile.id,
                VerifiedFactModel.state == ConfirmationState.CONFIRMED,
                or_(
                    VerifiedFactModel.resume_id == resume_id,
                    VerifiedFactModel.resume_id.is_(None),
                ),
            )
            .order_by(VerifiedFactModel.id)
        )
        grouped: dict[str, list[str]] = {}
        for model in models:
            grouped.setdefault(model.category, []).append(model.content)
        return {key: tuple(values) for key, values in grouped.items()}

    @staticmethod
    def _queries(
        supplied: tuple[str, ...] | None,
        facts: dict[str, tuple[str, ...]],
        resume_title: str,
    ) -> tuple[str, ...]:
        values = supplied or facts.get("desired_position", ()) or (resume_title,)
        normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("У активного резюме отсутствует название для поискового запроса")
        return normalized

    @staticmethod
    def _unique_regions(regions: tuple[SearchRegion, ...]) -> tuple[SearchRegion, ...]:
        return tuple({region.area: region for region in regions}.values())

    @staticmethod
    def _work_formats(
        supplied: tuple[WorkFormat, ...] | None,
        facts: dict[str, tuple[str, ...]],
    ) -> tuple[WorkFormat, ...]:
        if supplied is not None:
            return tuple(dict.fromkeys(supplied))
        found: list[WorkFormat] = []
        content = " ".join(facts.get("work_format", ())).casefold()
        if "удал" in content or "remote" in content:
            found.append(WorkFormat.REMOTE)
        if "офис" in content or "на месте" in content or "on-site" in content:
            found.append(WorkFormat.ON_SITE)
        if "гибрид" in content or "hybrid" in content:
            found.append(WorkFormat.HYBRID)
        return tuple(found)

    @staticmethod
    def _employment_forms(
        supplied: tuple[EmploymentForm, ...] | None,
        facts: dict[str, tuple[str, ...]],
    ) -> tuple[EmploymentForm, ...]:
        if supplied is not None:
            return tuple(dict.fromkeys(supplied))
        found: list[EmploymentForm] = []
        content = " ".join(facts.get("employment", ())).casefold()
        for marker, value in (
            ("полная", EmploymentForm.FULL),
            ("частич", EmploymentForm.PART),
            ("проект", EmploymentForm.PROJECT),
            ("вахт", EmploymentForm.FLY_IN_FLY_OUT),
        ):
            if marker in content:
                found.append(value)
        return tuple(found)

    @staticmethod
    def _salary_from_facts(
        facts: dict[str, tuple[str, ...]],
    ) -> tuple[int | None, int | None]:
        content = " ".join(facts.get("salary_expectation", ()))
        values = [
            int(re.sub(r"\D", "", match)) for match in re.findall(r"\d[\d\s\u00a0]{3,}", content)
        ]
        values = [value for value in values if value >= 10_000]
        if not values:
            return None, None
        if len(values) == 1:
            return None, values[0]
        return min(values), max(values)

    @staticmethod
    def _validate_salary(minimum: int | None, desired: int | None) -> None:
        if minimum is not None and minimum < 1:
            raise ValueError("Минимальная зарплата должна быть положительной")
        if desired is not None and desired < 1:
            raise ValueError("Желаемая зарплата должна быть положительной")
        if minimum is not None and desired is not None and minimum > desired:
            raise ValueError("Минимальная зарплата не может быть выше желаемой")

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            return None
        return int(value)
