"""Store the cover-letter quality gate result."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_cover_letter_quality"
down_revision: str | Sequence[str] | None = "0026_message_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cover_letters", sa.Column("quality_score", sa.Integer(), nullable=True))
    op.add_column("cover_letters", sa.Column("quality_passed", sa.Boolean(), nullable=True))
    op.add_column("cover_letters", sa.Column("quality_version", sa.String(64), nullable=True))
    op.add_column(
        "cover_letters",
        sa.Column("quality_model_name", sa.String(128), nullable=True),
    )
    op.add_column("cover_letters", sa.Column("quality_details", sa.JSON(), nullable=True))
    op.add_column(
        "cover_letters",
        sa.Column("quality_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_cover_letters_quality_score",
        "cover_letters",
        "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 10)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cover_letters_quality_score", "cover_letters", type_="check")
    op.drop_column("cover_letters", "quality_checked_at")
    op.drop_column("cover_letters", "quality_details")
    op.drop_column("cover_letters", "quality_model_name")
    op.drop_column("cover_letters", "quality_version")
    op.drop_column("cover_letters", "quality_passed")
    op.drop_column("cover_letters", "quality_score")
