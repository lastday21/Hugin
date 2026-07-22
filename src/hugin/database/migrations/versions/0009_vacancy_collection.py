"""Store vacancy availability, duplicate links, and change history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_vacancy_collection"
down_revision: str | Sequence[str] | None = "0008_resume_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "vacancies",
        sa.Column("availability", sa.String(length=16), nullable=False, server_default="ACTIVE"),
    )
    op.add_column("vacancies", sa.Column("duplicate_of_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "vacancy_availability",
        "vacancies",
        "availability IN ('ACTIVE', 'CLOSED', 'ARCHIVED', 'UNAVAILABLE')",
    )
    op.create_foreign_key(
        "fk_vacancies_duplicate_of_id",
        "vacancies",
        "vacancies",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_vacancies_duplicate_of_id", "vacancies", ["duplicate_of_id"])

    op.create_table(
        "vacancy_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "changes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["vacancy_id"], ["vacancies.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_vacancy_changes_vacancy_id", "vacancy_changes", ["vacancy_id"])


def downgrade() -> None:
    op.drop_index("ix_vacancy_changes_vacancy_id", table_name="vacancy_changes")
    op.drop_table("vacancy_changes")
    op.drop_index("ix_vacancies_duplicate_of_id", table_name="vacancies")
    op.drop_constraint("fk_vacancies_duplicate_of_id", "vacancies", type_="foreignkey")
    op.drop_constraint("vacancy_availability", "vacancies", type_="check")
    op.drop_column("vacancies", "duplicate_of_id")
    op.drop_column("vacancies", "availability")
