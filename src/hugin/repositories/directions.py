from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    CandidateProfileModel,
    CareerDirectionModel,
    DirectionResumeModel,
    DirectionSearchQueryModel,
    DirectionVacancyModel,
    HhAccountModel,
    ResumeModel,
    VacancyDiscoveryModel,
)
from hugin.domain.directions import (
    AccountRecord,
    ConfigPayload,
    DirectionRecord,
    DirectionVacancyRecord,
    ResumeRecord,
    SearchQueryRecord,
    SearchRegion,
    VacancyState,
    WorkFormat,
)
from hugin.domain.hh import HhResumeData
from hugin.domain.time import as_utc


def _account_record(model: HhAccountModel) -> AccountRecord:
    return AccountRecord(
        id=model.id,
        label=model.label,
        external_id=model.external_id,
        is_active=model.is_active,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _direction_record(model: CareerDirectionModel) -> DirectionRecord:
    return DirectionRecord(
        id=model.id,
        account_id=model.account_id,
        name=model.name,
        description=model.description,
        scoring_config=dict(model.scoring_config),
        is_active=model.is_active,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _query_record(model: DirectionSearchQueryModel) -> SearchQueryRecord:
    regions = tuple(
        SearchRegion(
            area=str(region.get("area", "")),
            name=str(region.get("name", region.get("area", ""))),
        )
        for region in model.regions
        if isinstance(region, dict) and region.get("area")
    )
    return SearchQueryRecord(
        id=model.id,
        direction_id=model.direction_id,
        query=model.query,
        area=model.area,
        filters=dict(model.filters),
        regions=regions,
        work_formats=tuple(WorkFormat(value) for value in model.work_formats),
        schedule_minutes=model.schedule_minutes,
        last_run_at=as_utc(model.last_run_at) if model.last_run_at is not None else None,
        next_run_at=as_utc(model.next_run_at) if model.next_run_at is not None else None,
        is_active=model.is_active,
        created_at=as_utc(model.created_at),
    )


def _resume_record(model: ResumeModel) -> ResumeRecord:
    return ResumeRecord(
        id=model.id,
        account_id=model.account_id,
        hh_id=model.hh_id,
        title=model.title,
        is_active=model.is_active,
        created_at=as_utc(model.created_at),
        updated_at=as_utc(model.updated_at),
    )


def _direction_vacancy_record(model: DirectionVacancyModel) -> DirectionVacancyRecord:
    return DirectionVacancyRecord(
        direction_id=model.direction_id,
        vacancy_id=model.vacancy_id,
        state=model.state,
        rules_score=model.rules_score,
        rules_details=dict(model.rules_details),
        ai_score=model.ai_score,
        fit_score=model.fit_score,
        first_seen_at=as_utc(model.first_seen_at),
        updated_at=as_utc(model.updated_at),
    )


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, label: str, external_id: str | None = None) -> AccountRecord:
        model = HhAccountModel(label=label, external_id=external_id)
        self._session.add(model)
        self._session.flush()
        return _account_record(model)

    def upsert(self, label: str, external_id: str) -> AccountRecord:
        model = self._session.scalar(
            select(HhAccountModel).where(HhAccountModel.external_id == external_id)
        )
        if model is None:
            model = HhAccountModel(external_id=external_id)
            self._session.add(model)
        model.label = label
        model.is_active = True
        self._session.flush()
        return _account_record(model)

    def get_by_external_id(self, external_id: str) -> AccountRecord | None:
        model = self._session.scalar(
            select(HhAccountModel).where(HhAccountModel.external_id == external_id)
        )
        return _account_record(model) if model is not None else None

    def get(self, account_id: int) -> AccountRecord:
        model = self._session.get(HhAccountModel, account_id)
        if model is None:
            raise LookupError("Аккаунт hh.ru не найден")
        return _account_record(model)


class ResumeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, account_id: int, hh_id: str, title: str) -> ResumeRecord:
        model = self._session.scalar(
            select(ResumeModel).where(
                ResumeModel.account_id == account_id,
                ResumeModel.hh_id == hh_id,
            )
        )
        if model is None:
            model = ResumeModel(account_id=account_id, hh_id=hh_id)
            self._session.add(model)
        model.title = title
        self._session.flush()
        return _resume_record(model)

    def synchronize(
        self,
        account_id: int,
        resumes: Sequence[HhResumeData],
    ) -> list[ResumeRecord]:
        existing = list(
            self._session.scalars(
                select(ResumeModel)
                .where(ResumeModel.account_id == account_id)
                .order_by(ResumeModel.id)
            )
        )
        by_hh_id = {resume.hh_id: resume for resume in existing}
        active_ids = {resume.hh_id for resume in resumes}

        for model in existing:
            model.is_active = model.hh_id in active_ids

        synchronized: list[ResumeModel] = []
        for resume in resumes:
            stored_model = by_hh_id.get(resume.hh_id)
            if stored_model is None:
                stored_model = ResumeModel(account_id=account_id, hh_id=resume.hh_id)
                self._session.add(stored_model)
            stored_model.title = resume.title
            stored_model.is_active = True
            synchronized.append(stored_model)

        self._session.flush()
        return [_resume_record(model) for model in synchronized]

    def list_by_account_id(self, account_id: int) -> list[ResumeRecord]:
        models = self._session.scalars(
            select(ResumeModel).where(ResumeModel.account_id == account_id).order_by(ResumeModel.id)
        )
        return [_resume_record(model) for model in models]

    def get(self, resume_id: int) -> ResumeRecord:
        model = self._session.get(ResumeModel, resume_id)
        if model is None:
            raise LookupError("resume was not found")
        return _resume_record(model)

    def get_active_by_title(self, account_id: int, title: str) -> ResumeRecord:
        models = list(
            self._session.scalars(
                select(ResumeModel).where(
                    ResumeModel.account_id == account_id,
                    ResumeModel.title == title,
                    ResumeModel.is_active.is_(True),
                )
            )
        )
        if not models:
            raise LookupError(f"Активное резюме «{title}» не найдено")
        if len(models) > 1:
            raise RuntimeError(f"Найдено несколько активных резюме «{title}»")
        return _resume_record(models[0])

    def get_profile_active(self, account_id: int) -> ResumeRecord:
        model = self._session.scalar(
            select(ResumeModel)
            .join(CandidateProfileModel, CandidateProfileModel.active_resume_id == ResumeModel.id)
            .where(
                CandidateProfileModel.account_id == account_id,
                ResumeModel.account_id == account_id,
                ResumeModel.is_active.is_(True),
            )
        )
        if model is None:
            raise LookupError(
                "Активное ИТ-резюме не найдено; сначала импортируйте и подтвердите резюме"
            )
        return _resume_record(model)


class DirectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        account_id: int,
        name: str,
        *,
        description: str | None = None,
        scoring_config: ConfigPayload | None = None,
    ) -> DirectionRecord:
        model = CareerDirectionModel(
            account_id=account_id,
            name=name,
            description=description,
            scoring_config=dict(scoring_config or {}),
        )
        self._session.add(model)
        self._session.flush()
        return _direction_record(model)

    def upsert(
        self,
        account_id: int,
        name: str,
        *,
        description: str | None = None,
        scoring_config: ConfigPayload | None = None,
    ) -> DirectionRecord:
        model = self._session.scalar(
            select(CareerDirectionModel).where(
                CareerDirectionModel.account_id == account_id,
                CareerDirectionModel.name == name,
            )
        )
        if model is None:
            model = CareerDirectionModel(account_id=account_id, name=name)
            self._session.add(model)
        if description is not None:
            model.description = description
        if scoring_config is not None:
            model.scoring_config = dict(scoring_config)
        model.is_active = True
        self._session.flush()
        return _direction_record(model)

    def get_by_account_and_name(self, account_id: int, name: str) -> DirectionRecord | None:
        model = self._session.scalar(
            select(CareerDirectionModel).where(
                CareerDirectionModel.account_id == account_id,
                CareerDirectionModel.name == name,
            )
        )
        return _direction_record(model) if model is not None else None

    def add_query(
        self,
        direction_id: int,
        query: str,
        *,
        area: str = "",
        filters: ConfigPayload | None = None,
        regions: Sequence[SearchRegion] = (),
        work_formats: Sequence[WorkFormat] = (),
        schedule_minutes: int = 120,
    ) -> SearchQueryRecord:
        model = DirectionSearchQueryModel(
            direction_id=direction_id,
            query=query,
            area=area,
            filters=dict(filters or {}),
            regions=[{"area": region.area, "name": region.name} for region in regions],
            work_formats=[work_format.value for work_format in work_formats],
            schedule_minutes=schedule_minutes,
        )
        self._session.add(model)
        self._session.flush()
        return _query_record(model)

    def upsert_query(
        self,
        direction_id: int,
        query: str,
        *,
        area: str = "",
        filters: ConfigPayload | None = None,
        regions: Sequence[SearchRegion] | None = None,
        work_formats: Sequence[WorkFormat] | None = None,
        schedule_minutes: int | None = None,
    ) -> SearchQueryRecord:
        model = self._session.scalar(
            select(DirectionSearchQueryModel).where(
                DirectionSearchQueryModel.direction_id == direction_id,
                DirectionSearchQueryModel.query == query,
                DirectionSearchQueryModel.area == area,
            )
        )
        if model is None:
            model = DirectionSearchQueryModel(
                direction_id=direction_id,
                query=query,
                area=area,
            )
            self._session.add(model)
        model.filters = dict(filters or {})
        if regions is not None:
            model.regions = [{"area": region.area, "name": region.name} for region in regions]
        if work_formats is not None:
            model.work_formats = [work_format.value for work_format in work_formats]
        if schedule_minutes is not None:
            if schedule_minutes < 5:
                raise ValueError("Интервал поиска должен быть не меньше 5 минут")
            model.schedule_minutes = schedule_minutes
        model.is_active = True
        self._session.flush()
        return _query_record(model)

    def get_query(self, query_id: int) -> SearchQueryRecord:
        model = self._session.get(DirectionSearchQueryModel, query_id)
        if model is None:
            raise LookupError("Настройка поискового запроса не найдена")
        return _query_record(model)

    def list_configured_queries(self, direction_id: int) -> list[SearchQueryRecord]:
        models = self._session.scalars(
            select(DirectionSearchQueryModel)
            .where(
                DirectionSearchQueryModel.direction_id == direction_id,
                DirectionSearchQueryModel.area == "",
                DirectionSearchQueryModel.is_active.is_(True),
            )
            .order_by(DirectionSearchQueryModel.id)
        )
        return [_query_record(model) for model in models]

    def disable_other_configured_queries(
        self,
        direction_id: int,
        active_query_ids: set[int],
    ) -> None:
        models = self._session.scalars(
            select(DirectionSearchQueryModel).where(
                DirectionSearchQueryModel.direction_id == direction_id,
                DirectionSearchQueryModel.area == "",
            )
        )
        for model in models:
            model.is_active = model.id in active_query_ids
        self._session.flush()

    def attach_resume(self, direction_id: int, resume_id: int, priority: int = 0) -> None:
        if priority < 0:
            raise ValueError("priority must not be negative")
        direction = self._session.get(CareerDirectionModel, direction_id)
        resume = self._session.get(ResumeModel, resume_id)
        if direction is None or resume is None:
            raise LookupError("direction or resume was not found")
        if direction.account_id != resume.account_id:
            raise ValueError("direction and resume must belong to the same account")

        link = self._session.get(DirectionResumeModel, (direction_id, resume_id))
        if link is None:
            link = DirectionResumeModel(direction_id=direction_id, resume_id=resume_id)
            self._session.add(link)
        link.priority = priority
        self._session.flush()

    def list_resumes(self, direction_id: int) -> list[ResumeRecord]:
        models = self._session.scalars(
            select(ResumeModel)
            .join(DirectionResumeModel)
            .where(DirectionResumeModel.direction_id == direction_id)
            .order_by(DirectionResumeModel.priority.desc(), ResumeModel.id)
        )
        return [_resume_record(model) for model in models]

    def track_vacancy(self, direction_id: int, vacancy_id: int) -> DirectionVacancyRecord:
        model = self._session.get(DirectionVacancyModel, (direction_id, vacancy_id))
        if model is None:
            model = DirectionVacancyModel(
                direction_id=direction_id,
                vacancy_id=vacancy_id,
                state=VacancyState.DISCOVERED,
            )
            self._session.add(model)
            self._session.flush()
        return _direction_vacancy_record(model)

    def record_discovery(
        self,
        *,
        direction_id: int,
        search_query_id: int,
        vacancy_id: int,
        query_text: str,
        region: str,
    ) -> None:
        model = self._session.scalar(
            select(VacancyDiscoveryModel).where(
                VacancyDiscoveryModel.vacancy_id == vacancy_id,
                VacancyDiscoveryModel.direction_id == direction_id,
                VacancyDiscoveryModel.query_text == query_text,
                VacancyDiscoveryModel.region == region,
            )
        )
        if model is None:
            self._session.add(
                VacancyDiscoveryModel(
                    vacancy_id=vacancy_id,
                    direction_id=direction_id,
                    search_query_id=search_query_id,
                    query_text=query_text,
                    region=region,
                )
            )
        else:
            model.search_query_id = search_query_id
        self._session.flush()

    def get_tracked_vacancy(
        self,
        direction_id: int,
        vacancy_id: int,
    ) -> DirectionVacancyRecord:
        model = self._session.get(DirectionVacancyModel, (direction_id, vacancy_id))
        if model is None:
            raise LookupError("direction vacancy was not found")
        return _direction_vacancy_record(model)

    def list_tracked_vacancies(self, direction_id: int) -> list[DirectionVacancyRecord]:
        models = self._session.scalars(
            select(DirectionVacancyModel)
            .where(DirectionVacancyModel.direction_id == direction_id)
            .order_by(
                DirectionVacancyModel.rules_score.desc().nulls_last(),
                DirectionVacancyModel.vacancy_id,
            )
        )
        return [_direction_vacancy_record(model) for model in models]

    def set_vacancy_state(
        self,
        direction_id: int,
        vacancy_id: int,
        state: VacancyState,
    ) -> DirectionVacancyRecord:
        model = self._session.get(DirectionVacancyModel, (direction_id, vacancy_id))
        if model is None:
            raise LookupError("direction vacancy was not found")
        model.state = state
        self._session.flush()
        return _direction_vacancy_record(model)

    def apply_rules(
        self,
        direction_id: int,
        vacancy_id: int,
        *,
        state: VacancyState,
        score: float,
        details: ConfigPayload,
    ) -> DirectionVacancyRecord:
        if not 0 <= score <= 100:
            raise ValueError("score must be between 0 and 100")
        model = self._session.get(DirectionVacancyModel, (direction_id, vacancy_id))
        if model is None:
            raise LookupError("direction vacancy was not found")
        model.state = state
        model.rules_score = score
        model.rules_details = dict(details)
        self._session.flush()
        return _direction_vacancy_record(model)
