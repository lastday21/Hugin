"""Preserve source-bound vacancy analysis stages for reuse and replay."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_semantic_stages"
down_revision: str | Sequence[str] | None = "0030_status_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_stages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("hh_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vacancy_id",
            sa.Integer(),
            sa.ForeignKey("vacancies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("response_sha256", sa.String(64), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.CheckConstraint("duration_seconds >= 0", name="ck_semantic_stages_duration"),
    )
    op.create_index(
        "ix_semantic_stages_lookup",
        "semantic_stages",
        ["account_id", "vacancy_id", "cache_key", "id"],
    )


def downgrade() -> None:
    op.drop_table("semantic_stages")
