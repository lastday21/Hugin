"""Create the task queue and system state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_queue_and_states"
down_revision: str | Sequence[str] | None = "0002_core_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPLICATION_EVENT_TYPES = ("APPLY_INTENT", "APPLIED", "UNKNOWN_RESULT", "STATE_CHANGED")
PREVIOUS_APPLICATION_EVENT_TYPES = ("APPLY_INTENT", "APPLIED", "UNKNOWN_RESULT")
TASK_STATES = (
    "PENDING",
    "RUNNING",
    "RETRY_SCHEDULED",
    "SKIPPED",
    "UNKNOWN_RESULT",
    "COMPLETED",
)
SYSTEM_STATES = (
    "RUNNING",
    "PAUSED",
    "AUTH_REQUIRED",
    "CAPTCHA_REQUIRED",
    "ACCOUNT_WARNING",
)


def values(values: tuple[str, ...]) -> str:
    return ", ".join(repr(value) for value in values)


def upgrade() -> None:
    with op.batch_alter_table("application_events") as batch:
        batch.drop_constraint("application_event_type", type_="check")
        batch.create_check_constraint(
            "application_event_type",
            f"event_type IN ({values(APPLICATION_EVENT_TYPES)})",
        )

    op.create_table(
        "application_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("priority_score", sa.Float(), nullable=False),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            f"state IN ({values(TASK_STATES)})",
            name="task_state",
        ),
        sa.CheckConstraint(
            "priority_score >= 0 AND priority_score <= 100",
            name="ck_application_tasks_priority_score",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_application_tasks_attempts"),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("application_id", name="uq_application_tasks_application_id"),
    )
    op.create_index(
        "ix_application_tasks_ready",
        "application_tasks",
        ["state", "scheduled_at", "priority_score"],
    )
    op.create_table(
        "system_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("id = 1", name="ck_system_state_singleton"),
        sa.CheckConstraint(
            f"state IN ({values(SYSTEM_STATES)})",
            name="system_state_value",
        ),
    )
    op.execute(sa.text("INSERT INTO system_state (id, state) VALUES (1, 'RUNNING')"))


def downgrade() -> None:
    op.drop_table("system_state")
    op.drop_index("ix_application_tasks_ready", table_name="application_tasks")
    op.drop_table("application_tasks")

    with op.batch_alter_table("application_events") as batch:
        batch.drop_constraint("application_event_type", type_="check")
        batch.create_check_constraint(
            "application_event_type",
            f"event_type IN ({values(PREVIOUS_APPLICATION_EVENT_TYPES)})",
        )
