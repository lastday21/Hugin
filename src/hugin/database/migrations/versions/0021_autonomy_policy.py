"""Add the saved autonomy policy and approved reply provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_autonomy_policy"
down_revision: str | Sequence[str] | None = "0020_profile_fact_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_POLICY = """{
  "auto_apply_stretch": true,
  "auto_submit_simple_forms": true,
  "auto_prepare_replies": true,
  "auto_send_approved_replies": true,
  "auto_reconcile_unknown": true,
  "reuse_confirmed_profile_facts": true,
  "mark_opened_invitations_seen": true,
  "mutable_fact_validity_days": 30,
  "reply_templates": []
}"""


def upgrade() -> None:
    op.add_column(
        "application_settings",
        sa.Column(
            "autonomy_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_POLICY}'::jsonb"),
        ),
    )
    op.add_column(
        "application_settings",
        sa.Column(
            "autonomy_policy_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_application_settings_autonomy_policy_version",
        "application_settings",
        "autonomy_policy_version >= 1",
    )
    op.add_column(
        "recruiter_messages",
        sa.Column(
            "auto_send_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "recruiter_messages",
        sa.Column("reply_template_key", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recruiter_messages", "reply_template_key")
    op.drop_column("recruiter_messages", "auto_send_approved")
    op.drop_constraint(
        "ck_application_settings_autonomy_policy_version",
        "application_settings",
        type_="check",
    )
    op.drop_column("application_settings", "autonomy_policy_version")
    op.drop_column("application_settings", "autonomy_policy")
