"""Keep the reason that prevents a confirmed questionnaire from being submitted."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_screening_confirmation"
down_revision: str | Sequence[str] | None = "0031_semantic_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("screening_forms", sa.Column("submission_block_reason", sa.Text()))


def downgrade() -> None:
    op.drop_column("screening_forms", "submission_block_reason")
