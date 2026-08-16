"""Track the last availability check for pending screening forms."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_screening_availability"
down_revision: str | Sequence[str] | None = "0024_profile_fact_actual_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_forms",
        sa.Column("availability_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("screening_forms", "availability_checked_at")
