"""Preserve cancelled replies and verified duplicate chat imports."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_message_lifecycle"
down_revision: str | Sequence[str] | None = "0035_captcha_agent_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS = "'RECEIVED','DRAFT','REVIEW_REQUIRED','CONFIRMED','SENT','FAILED','UNKNOWN_RESULT'"


def upgrade() -> None:
    for name in ("reply_to_message_id", "superseded_by_id"):
        op.add_column("recruiter_messages", sa.Column(name, sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_recruiter_messages_{name}",
            "recruiter_messages",
            "recruiter_messages",
            [name],
            ["id"],
            ondelete="SET NULL",
        )
    op.drop_constraint("recruiter_message_state", "recruiter_messages", type_="check")
    op.create_check_constraint(
        "recruiter_message_state",
        "recruiter_messages",
        f"state IN ({_PREVIOUS},'CANCELLED')",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM recruiter_messages WHERE state='CANCELLED')")
    ):
        raise RuntimeError(
            "Restore the pre-update backup before removing cancelled-message history"
        )
    op.drop_constraint("recruiter_message_state", "recruiter_messages", type_="check")
    op.create_check_constraint(
        "recruiter_message_state",
        "recruiter_messages",
        f"state IN ({_PREVIOUS})",
    )
    for name in ("superseded_by_id", "reply_to_message_id"):
        op.drop_constraint(
            f"fk_recruiter_messages_{name}", "recruiter_messages", type_="foreignkey"
        )
        op.drop_column("recruiter_messages", name)
