"""Store how a cover letter was selected or written."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_cover_letter_routing"
down_revision: str | Sequence[str] | None = "0022_system_recovery_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cover_letters",
        sa.Column(
            "generation_mode",
            sa.String(length=24),
            nullable=False,
            server_default="LEGACY",
        ),
    )
    op.add_column(
        "cover_letters",
        sa.Column("router_model_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "cover_letters",
        sa.Column("router_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "cover_letters",
        sa.Column("router_reason", sa.String(length=512), nullable=True),
    )
    op.create_check_constraint(
        "cover_letter_generation_mode",
        "cover_letters",
        "generation_mode IN ("
        "'LEGACY', 'MODEL_NEW', 'ROUTED_REUSE', 'LIGHT_EDIT', "
        "'DUPLICATE_REUSE', 'MANUAL')",
    )
    op.create_check_constraint(
        "ck_cover_letters_router_confidence",
        "cover_letters",
        "router_confidence IS NULL OR (router_confidence >= 0 AND router_confidence <= 1)",
    )
    op.alter_column("cover_letters", "generation_mode", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_cover_letters_router_confidence",
        "cover_letters",
        type_="check",
    )
    op.drop_constraint(
        "cover_letter_generation_mode",
        "cover_letters",
        type_="check",
    )
    op.drop_column("cover_letters", "router_reason")
    op.drop_column("cover_letters", "router_confidence")
    op.drop_column("cover_letters", "router_model_name")
    op.drop_column("cover_letters", "generation_mode")
