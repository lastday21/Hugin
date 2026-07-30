"""Add an exclusive lease for supervised applications."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_supervised_lease"
down_revision: str | Sequence[str] | None = "0016_safe_application_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "system_state",
        sa.Column("supervised_lease_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "system_state",
        sa.Column("supervised_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_system_state_supervised_lease_token",
        "system_state",
        ["supervised_lease_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_system_state_supervised_lease_token", table_name="system_state")
    op.drop_column("system_state", "supervised_lease_expires_at")
    op.drop_column("system_state", "supervised_lease_token")
