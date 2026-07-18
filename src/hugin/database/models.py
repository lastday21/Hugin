from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hugin.database.base import Base
from hugin.domain.applications import ApplicationEventType, ApplicationState, EventPayload
from hugin.domain.tasks import SystemState, TaskState


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_values(
    enum_type: type[ApplicationState]
    | type[ApplicationEventType]
    | type[TaskState]
    | type[SystemState],
) -> list[str]:
    return [member.value for member in enum_type]


class VacancyModel(Base):
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(primary_key=True)
    hh_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    employer_name: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    application: Mapped[ApplicationModel | None] = relationship(
        back_populates="vacancy",
        cascade="all, delete-orphan",
        single_parent=True,
    )


class ApplicationModel(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    resume_hh_id: Mapped[str] = mapped_column(String(64), nullable=False)
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
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    vacancy: Mapped[VacancyModel] = relationship(back_populates="application")
    events: Mapped[list[ApplicationEventModel]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationEventModel.id",
    )
    task: Mapped[ApplicationTaskModel | None] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        single_parent=True,
    )


class ApplicationEventModel(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
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
        Index(
            "ix_application_tasks_ready",
            "state",
            "scheduled_at",
            "priority_score",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
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
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
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
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
