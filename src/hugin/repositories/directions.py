from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import (
    CareerDirectionModel,
    DirectionResumeModel,
    DirectionSearchQueryModel,
    DirectionVacancyModel,
    HhAccountModel,
    ResumeModel,
)
from hugin.domain.directions import (
    AccountRecord,
    ConfigPayload,
    DirectionRecord,
    DirectionVacancyRecord,
    ResumeRecord,
    SearchQueryRecord,
    VacancyState,
)
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
    return SearchQueryRecord(
        id=model.id,
        direction_id=model.direction_id,
        query=model.query,
        area=model.area,
        filters=dict(model.filters),
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

    def add_query(
        self,
        direction_id: int,
        query: str,
        *,
        area: str = "",
        filters: ConfigPayload | None = None,
    ) -> SearchQueryRecord:
        model = DirectionSearchQueryModel(
            direction_id=direction_id,
            query=query,
            area=area,
            filters=dict(filters or {}),
        )
        self._session.add(model)
        self._session.flush()
        return _query_record(model)

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
