"""Persist the earliest time for the next application."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_queue_policy"
down_revision: str | Sequence[str] | None = "0009_vacancy_collection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "system_state",
        sa.Column("next_apply_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("system_state", "next_apply_at")
