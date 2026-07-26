"""Store durable background automation schedules."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_automation_jobs"
down_revision: str | Sequence[str] | None = "0011_cover_letter_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_jobs",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="WAITING"),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("hh_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "search_query_id",
            sa.Integer(),
            sa.ForeignKey("direction_search_queries.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column(
            "last_result",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('SEARCH', 'MESSAGES', 'STATUSES')",
            name="automation_job_kind",
        ),
        sa.CheckConstraint(
            "state IN ('WAITING', 'RUNNING', 'BLOCKED', 'FAILED', 'DISABLED')",
            name="automation_job_state",
        ),
        sa.CheckConstraint(
            "interval_seconds >= 1",
            name="ck_automation_jobs_interval_seconds",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_automation_jobs_consecutive_failures",
        ),
        sa.CheckConstraint(
            "(kind = 'SEARCH' AND search_query_id IS NOT NULL) "
            "OR (kind IN ('MESSAGES', 'STATUSES') AND search_query_id IS NULL)",
            name="ck_automation_jobs_scope",
        ),
    )
    op.create_index("ix_automation_jobs_account_id", "automation_jobs", ["account_id"])
    op.create_index(
        "ix_automation_jobs_search_query_id",
        "automation_jobs",
        ["search_query_id"],
        unique=True,
    )
    op.create_index(
        "ix_automation_jobs_due",
        "automation_jobs",
        ["state", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_table("automation_jobs")
