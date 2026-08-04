"""Store every rejected cover-letter draft with the exact reason."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_cover_letter_rejections"
down_revision: str | Sequence[str] | None = "0018_notification_cutoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cover_letter_rejections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cover_letter_id", sa.Integer(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("reason_message", sa.Text(), nullable=False),
        sa.Column("rejected_fragment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["cover_letter_id"],
            ["cover_letters.id"],
            name="fk_cover_letter_rejections_cover_letter_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cover_letter_id",
            "sequence_number",
            name="uq_cover_letter_rejections_letter_sequence",
        ),
    )
    op.create_index(
        "ix_cover_letter_rejections_cover_letter_id",
        "cover_letter_rejections",
        ["cover_letter_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cover_letter_rejections_cover_letter_id",
        table_name="cover_letter_rejections",
    )
    op.drop_table("cover_letter_rejections")
