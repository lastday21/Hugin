"""Keep a stable identity for each CAPTCHA episode."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_captcha_agent_event"
down_revision: str | Sequence[str] | None = "0034_application_candidate_pools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("system_state", sa.Column("captcha_case_id", sa.String(36), nullable=True))
    op.execute(
        "UPDATE system_state SET captcha_case_id = gen_random_uuid()::text "
        "WHERE state = 'CAPTCHA_REQUIRED'"
    )


def downgrade() -> None:
    op.drop_column("system_state", "captcha_case_id")
