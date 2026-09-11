"""Independent background controls and persisted execution state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_background_processes"
down_revision: str | Sequence[str] | None = "0032_screening_confirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("evaluation_enabled", "synchronization_enabled"):
        op.add_column(
            "application_settings",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.alter_column("application_settings", name, server_default=None)
    op.create_table(
        "background_process_runs",
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("hh_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False),
        sa.Column("check_now_pending", sa.Boolean(), nullable=False),
        sa.Column("cancel_generation", sa.Integer(), nullable=False),
        sa.Column("cursor_application_id", sa.Integer(), nullable=False),
        sa.Column("cursor_vacancy_id", sa.Integer()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("retry_after_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("background_process_runs")
    op.drop_column("application_settings", "synchronization_enabled")
    op.drop_column("application_settings", "evaluation_enabled")
