from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hugin.database.base import Base
from hugin.domain.applications import ApplicationEventType, ApplicationState, EventPayload
from hugin.domain.content import (
    AnswerSource,
    CompanyRuleType,
    ConfirmationState,
    CoverLetterState,
    DeliveryState,
    IncidentSeverity,
    IncidentState,
    InvitationState,
    MessageDirection,
    NotificationChannel,
    ProfileQuestionState,
    RecruiterMessageState,
    ResumeMappingRole,
    ScreeningFormState,
)
from hugin.domain.directions import ConfigPayload, VacancyState
from hugin.domain.tasks import SystemState, TaskState


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_values(enum_type: type[StrEnum]) -> list[str]:
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
        CheckConstraint(
            "schedule_minutes >= 5",
            name="ck_direction_search_queries_schedule_minutes",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    direction_id: Mapped[int] = mapped_column(
        ForeignKey("career_directions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    area: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    filters: Mapped[ConfigPayload] = mapped_column(JSONB, default=dict, nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    work_formats: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    schedule_minutes: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes > 0",
            name="ck_resumes_source_size_bytes",
        ),
        CheckConstraint(
            "source_page_count IS NULL OR source_page_count > 0",
            name="ck_resumes_source_page_count",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("hh_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    hh_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(32))
    source_reference: Mapped[str | None] = mapped_column(Text)
    source_original_name: Mapped[str | None] = mapped_column(String(255))
    source_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    source_size_bytes: Mapped[int | None] = mapped_column(Integer)
    source_page_count: Mapped[int | None] = mapped_column(Integer)
    content_text: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    role: Mapped[ResumeMappingRole] = mapped_column(
        Enum(
            ResumeMappingRole,
            name="resume_mapping_role",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=ResumeMappingRole.PRIMARY,
        nullable=False,
    )
    separate_application_allowed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
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
    region: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    salary_from: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_to: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_gross: Mapped[bool | None] = mapped_column(Boolean)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None] = mapped_column(Text)
    experience: Mapped[str | None] = mapped_column(String(128))
    employment: Mapped[str | None] = mapped_column(String(255))
    work_format: Mapped[str | None] = mapped_column(String(255))
    schedule: Mapped[str | None] = mapped_column(String(255))
    responsibilities: Mapped[str | None] = mapped_column(Text)
    required_qualifications: Mapped[str | None] = mapped_column(Text)
    preferred_qualifications: Mapped[str | None] = mapped_column(Text)
    has_cover_letter: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_screening_form: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_external_link: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_test_assignment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    key_skills: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    details_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
    rules_version: Mapped[str | None] = mapped_column(String(64))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class CandidateProfileModel(Base):
    __tablename__ = "candidate_profiles"
    __table_args__ = (UniqueConstraint("account_id", name="uq_candidate_profiles_account_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("hh_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    active_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "resumes.id",
            name="fk_candidate_profiles_active_resume",
            ondelete="SET NULL",
        ),
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProfileQuestionModel(Base):
    __tablename__ = "profile_questions"
    __table_args__ = (
        UniqueConstraint("profile_id", "key", name="uq_profile_questions_profile_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text)
    state: Mapped[ProfileQuestionState] = mapped_column(
        Enum(
            ProfileQuestionState,
            name="profile_question_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=ProfileQuestionState.PENDING,
        nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class VerifiedFactModel(Base):
    __tablename__ = "verified_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text)
    actual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), index=True
    )
    direction_id: Mapped[int | None] = mapped_column(
        ForeignKey("career_directions.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[ConfirmationState] = mapped_column(
        Enum(
            ConfirmationState,
            name="fact_confirmation_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=ConfirmationState.PENDING,
        nullable=False,
    )
    allow_in_letters: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_in_forms: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_in_messages: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class CompanyRuleModel(Base):
    __tablename__ = "company_rules"
    __table_args__ = (
        UniqueConstraint(
            "direction_id",
            "company_pattern",
            "rule_type",
            name="uq_company_rules_direction_pattern_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    direction_id: Mapped[int] = mapped_column(
        ForeignKey("career_directions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[CompanyRuleType] = mapped_column(
        Enum(
            CompanyRuleType,
            name="company_rule_type",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class VacancyDiscoveryModel(Base):
    __tablename__ = "vacancy_discoveries"
    __table_args__ = (
        UniqueConstraint(
            "vacancy_id",
            "direction_id",
            "query_text",
            "region",
            name="uq_vacancy_discoveries_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction_id: Mapped[int | None] = mapped_column(
        ForeignKey("career_directions.id", ondelete="SET NULL"), index=True
    )
    search_query_id: Mapped[int | None] = mapped_column(
        ForeignKey("direction_search_queries.id", ondelete="SET NULL"), index=True
    )
    query_text: Mapped[str] = mapped_column(String(512), nullable=False)
    region: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class PromptVersionModel(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("purpose", "version", name="uq_prompt_versions_purpose_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    instruction_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CoverLetterModel(Base):
    __tablename__ = "cover_letters"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "instruction_version",
            name="uq_cover_letters_application_instruction",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    direction_id: Mapped[int | None] = mapped_column(
        ForeignKey("career_directions.id", ondelete="SET NULL"), index=True
    )
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    prompt_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )
    text: Mapped[str | None] = mapped_column(Text)
    instruction_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[CoverLetterState] = mapped_column(
        Enum(
            CoverLetterState,
            name="cover_letter_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=CoverLetterState.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoverLetterFactModel(Base):
    __tablename__ = "cover_letter_facts"

    cover_letter_id: Mapped[int] = mapped_column(
        ForeignKey("cover_letters.id", ondelete="CASCADE"), primary_key=True
    )
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("verified_facts.id", ondelete="RESTRICT"), primary_key=True
    )


class ScreeningFormModel(Base):
    __tablename__ = "screening_forms"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "version_hash",
            name="uq_screening_forms_application_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[ScreeningFormState] = mapped_column(
        Enum(
            ScreeningFormState,
            name="screening_form_state",
            native_enum=False,
            create_constraint=True,
            length=24,
            values_callable=enum_values,
        ),
        default=ScreeningFormState.DRAFT,
        nullable=False,
    )
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ScreeningQuestionModel(Base):
    __tablename__ = "screening_questions"
    __table_args__ = (
        UniqueConstraint("form_id", "field_key", name="uq_screening_questions_form_key"),
        CheckConstraint("position >= 0", name="ck_screening_questions_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(
        ForeignKey("screening_forms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_key: Mapped[str] = mapped_column(String(255), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    field_type: Mapped[str] = mapped_column(String(64), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    max_length: Mapped[int | None] = mapped_column(Integer)
    format_hint: Mapped[str | None] = mapped_column(String(255))
    has_attachment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_external_action: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_test_assignment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ScreeningAnswerModel(Base):
    __tablename__ = "screening_answers"
    __table_args__ = (UniqueConstraint("question_id", name="uq_screening_answers_question_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("screening_questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    answer_text: Mapped[str | None] = mapped_column(Text)
    source: Mapped[AnswerSource | None] = mapped_column(
        Enum(
            AnswerSource,
            name="screening_answer_source",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        )
    )
    verified_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("verified_facts.id", ondelete="SET NULL")
    )
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AnswerTemplateModel(Base):
    __tablename__ = "answer_templates"
    __table_args__ = (
        UniqueConstraint("profile_id", "key", name="uq_answer_templates_profile_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    question_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    verified_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("verified_facts.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class RecruiterMessageModel(Base):
    __tablename__ = "recruiter_messages"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "hh_id",
            name="uq_recruiter_messages_application_hh_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hh_id: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(
            MessageDirection,
            name="message_direction",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[RecruiterMessageState] = mapped_column(
        Enum(
            RecruiterMessageState,
            name="recruiter_message_state",
            native_enum=False,
            create_constraint=True,
            length=24,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RecruiterMessageFactModel(Base):
    __tablename__ = "recruiter_message_facts"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("recruiter_messages.id", ondelete="CASCADE"), primary_key=True
    )
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("verified_facts.id", ondelete="RESTRICT"), primary_key=True
    )


class InvitationModel(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint("application_id", "hh_id", name="uq_invitations_application_hh_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hh_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    interview_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    booking_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[InvitationState] = mapped_column(
        Enum(
            InvitationState,
            name="invitation_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=InvitationState.RECEIVED,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class IncidentModel(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[IncidentSeverity] = mapped_column(
        Enum(
            IncidentSeverity,
            name="incident_severity",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    state: Mapped[IncidentState] = mapped_column(
        Enum(
            IncidentState,
            name="incident_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=IncidentState.OPEN,
        nullable=False,
    )
    scope_type: Mapped[str | None] = mapped_column(String(64))
    scope_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    diagnostic_path: Mapped[str | None] = mapped_column(Text)
    snapshot_path: Mapped[str | None] = mapped_column(Text)
    trace_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationModel(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(
            NotificationChannel,
            name="notification_channel",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    state: Mapped[DeliveryState] = mapped_column(
        Enum(
            DeliveryState,
            name="notification_delivery_state",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=DeliveryState.PENDING,
        nullable=False,
    )
    payload: Mapped[ConfigPayload] = mapped_column(JSONB, default=dict, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ApplicationSettingsModel(Base):
    __tablename__ = "application_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_application_settings_singleton"),
        CheckConstraint(
            "hh_apply_daily_limit >= 25",
            name="ck_application_settings_daily_limit",
        ),
        CheckConstraint(
            "hh_apply_delay_min_seconds >= 0",
            name="ck_application_settings_delay_min",
        ),
        CheckConstraint(
            "hh_apply_delay_max_seconds >= hh_apply_delay_min_seconds",
            name="ck_application_settings_delay_order",
        ),
        CheckConstraint(
            "search_interval_minutes >= 5",
            name="ck_application_settings_search_interval",
        ),
        CheckConstraint(
            "message_interval_minutes >= 1 AND status_interval_minutes >= 1",
            name="ck_application_settings_sync_intervals",
        ),
        CheckConstraint(
            "diagnostics_retention_days >= 1 AND logs_retention_days >= 1 "
            "AND backups_retention_days >= 1",
            name="ck_application_settings_retention",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    timezone_name: Mapped[str] = mapped_column(String(128), nullable=False)
    search_interval_minutes: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    message_interval_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    status_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    hh_apply_daily_limit: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    hh_apply_delay_min_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    hh_apply_delay_max_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    windows_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notification_routing: Mapped[ConfigPayload] = mapped_column(JSONB, default=dict, nullable=False)
    diagnostics_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    logs_retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    backups_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
