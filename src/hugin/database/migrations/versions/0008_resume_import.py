"""Add local resume import metadata and reusable profile questions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_resume_import"
down_revision: str | Sequence[str] | None = "0007_v03_data_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("source_original_name", sa.String(length=255)))
    op.add_column("resumes", sa.Column("source_sha256", sa.String(length=64)))
    op.add_column("resumes", sa.Column("source_size_bytes", sa.Integer()))
    op.add_column("resumes", sa.Column("source_page_count", sa.Integer()))
    op.create_index("ix_resumes_source_sha256", "resumes", ["source_sha256"])
    op.create_check_constraint(
        "ck_resumes_source_size_bytes",
        "resumes",
        "source_size_bytes IS NULL OR source_size_bytes > 0",
    )
    op.create_check_constraint(
        "ck_resumes_source_page_count",
        "resumes",
        "source_page_count IS NULL OR source_page_count > 0",
    )

    op.add_column("candidate_profiles", sa.Column("active_resume_id", sa.Integer()))
    op.create_index(
        "ix_candidate_profiles_active_resume_id",
        "candidate_profiles",
        ["active_resume_id"],
    )
    op.create_foreign_key(
        "fk_candidate_profiles_active_resume",
        "candidate_profiles",
        "resumes",
        ["active_resume_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "profile_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text()),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'ANSWERED', 'DISMISSED')",
            name="profile_question_state",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["candidate_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "profile_id",
            "key",
            name="uq_profile_questions_profile_key",
        ),
    )
    op.create_index("ix_profile_questions_profile_id", "profile_questions", ["profile_id"])

    op.create_unique_constraint(
        "uq_answer_templates_profile_key",
        "answer_templates",
        ["profile_id", "key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_answer_templates_profile_key",
        "answer_templates",
        type_="unique",
    )
    op.drop_index("ix_profile_questions_profile_id", table_name="profile_questions")
    op.drop_table("profile_questions")
    op.drop_constraint(
        "fk_candidate_profiles_active_resume",
        "candidate_profiles",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_candidate_profiles_active_resume_id",
        table_name="candidate_profiles",
    )
    op.drop_column("candidate_profiles", "active_resume_id")
    op.drop_constraint("ck_resumes_source_page_count", "resumes", type_="check")
    op.drop_constraint("ck_resumes_source_size_bytes", "resumes", type_="check")
    op.drop_index("ix_resumes_source_sha256", table_name="resumes")
    op.drop_column("resumes", "source_page_count")
    op.drop_column("resumes", "source_size_bytes")
    op.drop_column("resumes", "source_sha256")
    op.drop_column("resumes", "source_original_name")
