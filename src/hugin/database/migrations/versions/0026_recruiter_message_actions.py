"""Store recruiter-message actions separately from message delivery state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_message_actions"
down_revision: str | Sequence[str] | None = "0025_screening_availability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recruiter_message_actions",
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "REPLY",
                "EXTERNAL_FORM",
                "TEST_ASSIGNMENT",
                "EXTERNAL_ACTION",
                name="recruiter_action_kind",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "REQUIRED",
                "COMPLETED",
                "DISMISSED",
                "NOT_REQUIRED",
                name="recruiter_action_state",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "RULE",
                "MODEL",
                "USER",
                "SYSTEM",
                name="recruiter_action_source",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "(state = 'REQUIRED' AND resolved_at IS NULL) OR "
            "(state != 'REQUIRED' AND resolved_at IS NOT NULL)",
            name="ck_recruiter_message_actions_resolution",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["recruiter_messages.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", "kind"),
    )
    op.create_index(
        "ix_recruiter_message_actions_due",
        "recruiter_message_actions",
        ["state", "due_at"],
    )


def downgrade() -> None:
    op.drop_table("recruiter_message_actions")
