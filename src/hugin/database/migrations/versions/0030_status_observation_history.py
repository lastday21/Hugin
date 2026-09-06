"""Keep the history of statuses actually observed on hh.ru."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_status_history"
down_revision: str | Sequence[str] | None = "0029_status_observation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_status_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "APPLYING",
                "APPLIED",
                "VIEWED",
                "INVITED",
                "REJECTED",
                "CLOSED",
                name="observed_application_state",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("status_label", sa.String(255), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "application_id", "checked_at", "state", name="uq_application_status_observations_check"
        ),
    )
    op.create_index(
        "ix_application_status_observations_application_time",
        "application_status_observations",
        ["application_id", "checked_at"],
    )


def downgrade() -> None:
    op.drop_table("application_status_observations")
