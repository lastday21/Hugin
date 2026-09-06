"""Record when an application status was observed on hh.ru."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_status_observation"
down_revision: str | Sequence[str] | None = "0028_development_dashboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applications", sa.Column("status_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "application_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interview_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interview_evidence", sa.Text(), nullable=False),
        sa.Column("rejection_reason", sa.String(500), nullable=False),
        sa.Column("rejection_evidence", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_application_outcomes_application_id", "application_outcomes", ["application_id"]
    )


def downgrade() -> None:
    op.drop_table("application_outcomes")
    op.drop_column("applications", "status_checked_at")
