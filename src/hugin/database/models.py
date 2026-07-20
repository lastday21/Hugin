from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hugin.database.base import Base
from hugin.domain.applications import ApplicationEventType, ApplicationState, EventPayload
from hugin.domain.directions import ConfigPayload, VacancyState
from hugin.domain.tasks import SystemState, TaskState


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_values(
    enum_type: type[ApplicationState]
    | type[ApplicationEventType]
    | type[TaskState]
    | type[SystemState]
    | type[VacancyState],
) -> list[str]:
    return [member.value for member in enum_type]


class HhAccountModel(Base):
    __tablename__ = "hh_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    directions: Mapped[list[CareerDirectionModel]] = relationship(back_populates="account")
    resumes: Mapped[list[ResumeModel]] = relationship(back_populates="account")
    applications: Mapped[list[ApplicationModel]] = relationship(back_populates="account")


class CareerDirectionModel(Base):
    __tablename__ = "career_directions"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_career_directions_account_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("hh_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    scoring_config: Mapped[ConfigPayload] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    account: Mapped[HhAccountModel] = relationship(back_populates="directions")
    queries: Mapped[list[DirectionSearchQueryModel]] = relationship(
        back_populates="direction", cascade="all, delete-orphan"
    )
    resume_links: Mapped[list[DirectionResumeModel]] = relationship(
        back_populates="direction", cascade="all, delete-orphan"
    )
    vacancy_links: Mapped[list[DirectionVacancyModel]] = relationship(
        back_populates="direction", cascade="all, delete-orphan"
    )
    applications: Mapped[list[ApplicationModel]] = relationship(back_populates="direction")


class DirectionSearchQueryModel(Base):
    __tablename__ = "direction_search_queries"
    __table_args__ = (
        UniqueConstraint(
            "direction_id",
            "query",
            "area",
            name="uq_direction_search_queries_direction_query_area",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    direction_id: Mapped[int] = mapped_column(
        ForeignKey("career_directions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    area: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    filters: Mapped[ConfigPayload] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    direction: Mapped[CareerDirectionModel] = relationship(back_populates="queries")


class ResumeModel(Base):
    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("account_id", "hh_id", name="uq_resumes_account_hh_id"),
        UniqueConstraint("account_id", "id", name="uq_resumes_account_id_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("hh_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    hh_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    account: Mapped[HhAccountModel] = relationship(back_populates="resumes")
    direction_links: Mapped[list[DirectionResumeModel]] = relationship(
        back_populates="resume", cascade="all, delete-orphan"
    )
    applications: Mapped[list[ApplicationModel]] = relationship(
        back_populates="resume",
        primaryjoin="ResumeModel.id == ApplicationModel.resume_id",
        foreign_keys="ApplicationModel.resume_id",
    )


class DirectionResumeModel(Base):
    __tablename__ = "direction_resumes"
    __table_args__ = (CheckConstraint("priority >= 0", name="ck_direction_resumes_priority"),)

    direction_id: Mapped[int] = mapped_column(
        ForeignKey("career_directions.id", ondelete="CASCADE"), primary_key=True
    )
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), primary_key=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    direction: Mapped[CareerDirectionModel] = relationship(back_populates="resume_links")
    resume: Mapped[ResumeModel] = relationship(back_populates="direction_links")


class VacancyModel(Base):
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(primary_key=True)
    hh_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    employer_name: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None] = mapped_column(Text)
    experience: Mapped[str | None] = mapped_column(String(128))
    employment: Mapped[str | None] = mapped_column(String(255))
    work_format: Mapped[str | None] = mapped_column(String(255))
    key_skills: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    details_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    direction_links: Mapped[list[DirectionVacancyModel]] = relationship(
        back_populates="vacancy", cascade="all, delete-orphan"
    )
    applications: Mapped[list[ApplicationModel]] = relationship(
        back_populates="vacancy", cascade="all, delete-orphan"
    )


class DirectionVacancyModel(Base):
    __tablename__ = "direction_vacancies"
    __table_args__ = (
        CheckConstraint(
            "rules_score IS NULL OR (rules_score >= 0 AND rules_score <= 100)",
            name="ck_direction_vacancies_rules_score",
        ),
        CheckConstraint(
            "ai_score IS NULL OR (ai_score >= 0 AND ai_score <= 100)",
            name="ck_direction_vacancies_ai_score",
        ),
        CheckConstraint(
            "fit_score IS NULL OR (fit_score >= 0 AND fit_score <= 100)",
            name="ck_direction_vacancies_fit_score",
        ),
        Index("ix_direction_vacancies_queue", "direction_id", "state", "fit_score"),
    )

    direction_id: Mapped[int] = mapped_column(
        ForeignKey("career_directions.id", ondelete="CASCADE"), primary_key=True
    )
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[VacancyState] = mapped_column(
        Enum(
            VacancyState,
            name="vacancy_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=VacancyState.DISCOVERED,
        nullable=False,
    )
    rules_score: Mapped[float | None] = mapped_column(Float)
    rules_details: Mapped[ConfigPayload] = mapped_column(JSONB, default=dict, nullable=False)
    ai_score: Mapped[float | None] = mapped_column(Float)
    fit_score: Mapped[float | None] = mapped_column(Float)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    direction: Mapped[CareerDirectionModel] = relationship(back_populates="vacancy_links")
    vacancy: Mapped[VacancyModel] = relationship(back_populates="direction_links")


class ApplicationModel(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "vacancy_id",
            "resume_id",
            name="uq_applications_account_vacancy_resume",
        ),
        ForeignKeyConstraint(
            ["account_id", "resume_id"],
            ["resumes.account_id", "resumes.id"],
            name="fk_applications_account_resume",
            ondelete="RESTRICT",
        ),
        Index("ix_applications_vacancy_id", "vacancy_id"),
        Index("ix_applications_direction_id", "direction_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("hh_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False
    )
    resume_id: Mapped[int] = mapped_column(nullable=False)
    direction_id: Mapped[int | None] = mapped_column(
        ForeignKey("career_directions.id", ondelete="SET NULL")
    )
    state: Mapped[ApplicationState] = mapped_column(
        Enum(
            ApplicationState,
            name="application_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=ApplicationState.APPLYING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    account: Mapped[HhAccountModel] = relationship(back_populates="applications")
    vacancy: Mapped[VacancyModel] = relationship(back_populates="applications")
    resume: Mapped[ResumeModel] = relationship(
        back_populates="applications",
        primaryjoin="ResumeModel.id == ApplicationModel.resume_id",
        foreign_keys=[resume_id],
    )
    direction: Mapped[CareerDirectionModel | None] = relationship(back_populates="applications")
    events: Mapped[list[ApplicationEventModel]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationEventModel.id",
    )
    task: Mapped[ApplicationTaskModel | None] = relationship(
        back_populates="application", cascade="all, delete-orphan", single_parent=True
    )


class ApplicationEventModel(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[ApplicationEventType] = mapped_column(
        Enum(
            ApplicationEventType,
            name="application_event_type",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    payload: Mapped[EventPayload] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    application: Mapped[ApplicationModel] = relationship(back_populates="events")


class ApplicationTaskModel(Base):
    __tablename__ = "application_tasks"
    __table_args__ = (
        CheckConstraint(
            "priority_score >= 0 AND priority_score <= 100",
            name="ck_application_tasks_priority_score",
        ),
        CheckConstraint("attempts >= 0", name="ck_application_tasks_attempts"),
        Index("ix_application_tasks_ready", "state", "scheduled_at", "priority_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    state: Mapped[TaskState] = mapped_column(
        Enum(
            TaskState,
            name="task_state",
            native_enum=False,
            create_constraint=True,
            length=24,
            values_callable=enum_values,
        ),
        default=TaskState.PENDING,
        nullable=False,
    )
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    application: Mapped[ApplicationModel] = relationship(back_populates="task")


class SystemStateModel(Base):
    __tablename__ = "system_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_system_state_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    state: Mapped[SystemState] = mapped_column(
        Enum(
            SystemState,
            name="system_state_value",
            native_enum=False,
            create_constraint=True,
            length=24,
            values_callable=enum_values,
        ),
        default=SystemState.RUNNING,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
