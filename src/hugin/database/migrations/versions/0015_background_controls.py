"""Add background search and resource saving controls."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_background_controls"
down_revision: str | Sequence[str] | None = "0014_ai_prompt_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_settings",
        sa.Column(
            "search_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "application_settings",
        sa.Column(
            "resource_saving_mode",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("application_settings", "resource_saving_mode")
    op.drop_column("application_settings", "search_enabled")
