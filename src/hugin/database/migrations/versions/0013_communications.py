"""Add durable communication state and notification deduplication."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_communications"
down_revision: str | Sequence[str] | None = "0012_automation_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recruiter_messages",
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "recruiter_messages",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "recruiter_messages",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_recruiter_messages_version",
        "recruiter_messages",
        "version >= 1",
    )
    op.drop_constraint(
        "recruiter_message_state",
        "recruiter_messages",
        type_="check",
    )
    op.create_check_constraint(
        "recruiter_message_state",
        "recruiter_messages",
        "state IN ('RECEIVED', 'DRAFT', 'REVIEW_REQUIRED', 'CONFIRMED', "
        "'SENT', 'FAILED', 'UNKNOWN_RESULT')",
    )

    op.add_column(
        "invitations",
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "notifications",
        sa.Column("deduplication_key", sa.String(length=128), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE notifications "
            "SET deduplication_key = 'legacy:' || id::text "
            "WHERE deduplication_key IS NULL"
        )
    )
    op.alter_column(
        "notifications",
        "deduplication_key",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_notifications_deduplication_key",
        "notifications",
        ["deduplication_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_notifications_deduplication_key",
        "notifications",
        type_="unique",
    )
    op.drop_column("notifications", "deduplication_key")

    op.drop_column("invitations", "seen_at")

    op.drop_constraint(
        "recruiter_message_state",
        "recruiter_messages",
        type_="check",
    )
    op.execute(
        sa.text("UPDATE recruiter_messages SET state = 'FAILED' WHERE state = 'UNKNOWN_RESULT'")
    )
    op.create_check_constraint(
        "recruiter_message_state",
        "recruiter_messages",
        "state IN ('RECEIVED', 'DRAFT', 'REVIEW_REQUIRED', 'CONFIRMED', 'SENT', 'FAILED')",
    )
    op.drop_constraint(
        "ck_recruiter_messages_version",
        "recruiter_messages",
        type_="check",
    )
    op.drop_column("recruiter_messages", "version")
    op.drop_column("recruiter_messages", "content_hash")
    op.drop_column("recruiter_messages", "read_at")
