"""Add the product development dashboard."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_development_dashboard"
down_revision: str | Sequence[str] | None = "0027_cover_letter_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "development_directions",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("block_key", sa.String(64), nullable=False),
        sa.Column("block_name", sa.String(255), nullable=False),
        sa.Column("block_position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column(
            "criticality",
            sa.Enum(
                "LOW",
                "MEDIUM",
                "HIGH",
                name="development_quality_level",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "block_key",
            "position",
            name="uq_development_directions_block_position",
        ),
    )
    op.create_index(
        "ix_development_directions_block_key",
        "development_directions",
        ["block_key"],
    )

    op.create_table(
        "development_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "direction_key",
            sa.String(64),
            sa.ForeignKey("development_directions.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "confidence",
            sa.Enum(
                "LOW",
                "MEDIUM",
                "HIGH",
                name="development_assessment_confidence",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("next_step", sa.Text(), nullable=False),
        sa.Column("author", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 5",
            name="ck_development_assessments_score",
        ),
    )
    op.create_index(
        "ix_development_assessments_direction_key",
        "development_assessments",
        ["direction_key"],
    )
    op.create_index(
        "ix_development_assessments_created_at",
        "development_assessments",
        ["created_at"],
    )

    op.create_table(
        "development_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_key", sa.String(64), nullable=True, unique=True),
        sa.Column(
            "kind",
            sa.Enum(
                "PROBLEM",
                "TASK",
                "HYPOTHESIS",
                "MEASUREMENT",
                "CHECK",
                "IMPROVEMENT",
                name="development_item_kind",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column(
            "direction_key",
            sa.String(64),
            sa.ForeignKey("development_directions.key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "IDEA",
                "PLANNED",
                "IN_PROGRESS",
                "VERIFYING",
                "DONE",
                "REJECTED",
                "WAITING_EXTERNAL",
                name="development_item_status",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Enum(
                "UNASSIGNED",
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
                name="development_priority",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("expected_metric", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), server_default="", nullable=False),
        sa.Column("verification_method", sa.Text(), server_default="", nullable=False),
        sa.Column("next_step", sa.Text(), server_default="", nullable=False),
        sa.Column("actual_result", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "reference_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("author", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_development_items_direction_key",
        "development_items",
        ["direction_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_development_items_direction_key", table_name="development_items")
    op.drop_table("development_items")
    op.drop_index(
        "ix_development_assessments_created_at",
        table_name="development_assessments",
    )
    op.drop_index(
        "ix_development_assessments_direction_key",
        table_name="development_assessments",
    )
    op.drop_table("development_assessments")
    op.drop_index(
        "ix_development_directions_block_key",
        table_name="development_directions",
    )
    op.drop_table("development_directions")
