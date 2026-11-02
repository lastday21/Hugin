"""Persist the candidate set admitted before daily applications."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_application_candidate_pools"
down_revision: str | Sequence[str] | None = "0033_background_processes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_candidate_pools",
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("hh_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day_started_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("configuration_key", sa.String(64), primary_key=True),
        sa.Column("vacancy_ids", postgresql.JSONB(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("application_candidate_pools")
