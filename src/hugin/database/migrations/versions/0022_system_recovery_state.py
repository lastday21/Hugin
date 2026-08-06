"""Remember the queue mode across authentication challenges."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_system_recovery_state"
down_revision: str | Sequence[str] | None = "0021_autonomy_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "system_state",
        sa.Column("recovery_state", sa.String(length=24), nullable=True),
    )
    op.create_check_constraint(
        "ck_system_state_recovery_state",
        "system_state",
        "recovery_state IS NULL OR recovery_state IN ('RUNNING', 'PAUSED')",
    )
    op.execute(
        sa.text(
            "UPDATE system_state "
            "SET recovery_state = 'PAUSED' "
            "WHERE state IN ('AUTH_REQUIRED', 'CAPTCHA_REQUIRED')"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_system_state_recovery_state",
        "system_state",
        type_="check",
    )
    op.drop_column("system_state", "recovery_state")
