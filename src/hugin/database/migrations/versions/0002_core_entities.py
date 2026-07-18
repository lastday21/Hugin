"""Create vacancies, applications and application events."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_core_entities"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPLICATION_STATES = (
    "DISCOVERED",
    "FILTERED",
    "ANALYZED",
    "QUEUED",
    "APPLYING",
    "APPLIED",
    "VIEWED",
    "INVITED",
    "REJECTED",
    "CLOSED",
)
APPLICATION_EVENT_TYPES = ("APPLY_INTENT", "APPLIED", "UNKNOWN_RESULT")


def upgrade() -> None:
    op.create_table(
        "vacancies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hh_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("employer_name", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("hh_id", name="uq_vacancies_hh_id"),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("resume_hh_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"state IN ({', '.join(repr(value) for value in APPLICATION_STATES)})",
            name="application_state",
        ),
        sa.ForeignKeyConstraint(["vacancy_id"], ["vacancies.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("vacancy_id", name="uq_applications_vacancy_id"),
    )
    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"event_type IN ({', '.join(repr(value) for value in APPLICATION_EVENT_TYPES)})",
            name="application_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_application_events_application_id",
        "application_events",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_application_events_application_id", table_name="application_events")
    op.drop_table("application_events")
    op.drop_table("applications")
    op.drop_table("vacancies")
