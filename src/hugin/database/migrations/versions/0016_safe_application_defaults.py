"""Keep application sending paused until the user enables it."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_safe_application_defaults"
down_revision: str | Sequence[str] | None = "0015_background_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE system_state "
            "SET state = 'PAUSED', next_apply_at = NULL "
            "WHERE id = 1 AND state = 'RUNNING'"
        )
    )


def downgrade() -> None:
    # Откат структуры не должен сам включать отправку откликов.
    pass
