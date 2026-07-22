"""Store cover-letter generation context and failures."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_cover_letter_generation"
down_revision: str | Sequence[str] | None = "0010_queue_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cover_letters",
        sa.Column("reused_from_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "cover_letters",
        sa.Column("context_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "cover_letters",
        sa.Column("failure_reason", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "cover_letters",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_foreign_key(
        "fk_cover_letters_reused_from_id",
        "cover_letters",
        "cover_letters",
        ["reused_from_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_cover_letters_reused_from_id",
        "cover_letters",
        ["reused_from_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cover_letters_reused_from_id", table_name="cover_letters")
    op.drop_constraint(
        "fk_cover_letters_reused_from_id",
        "cover_letters",
        type_="foreignkey",
    )
    op.drop_column("cover_letters", "updated_at")
    op.drop_column("cover_letters", "failure_reason")
    op.drop_column("cover_letters", "context_hash")
    op.drop_column("cover_letters", "reused_from_id")
