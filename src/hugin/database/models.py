from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hugin.database.base import Base
from hugin.domain.applications import ApplicationEventType, ApplicationState, EventPayload


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_values(enum_type: type[ApplicationState] | type[ApplicationEventType]) -> list[str]:
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
