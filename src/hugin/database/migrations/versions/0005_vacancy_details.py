"""Store vacancy details and rule evaluation reasons."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_vacancy_details"
down_revision: str | Sequence[str] | None = "0004_directions_and_resumes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vacancies", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("vacancies", sa.Column("experience", sa.String(length=128), nullable=True))
    op.add_column("vacancies", sa.Column("employment", sa.String(length=255), nullable=True))
    op.add_column("vacancies", sa.Column("work_format", sa.String(length=255), nullable=True))
    op.add_column(
        "vacancies",
        sa.Column(
            "key_skills",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "vacancies",
        sa.Column("details_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "direction_vacancies",
        sa.Column(
            "rules_details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("direction_vacancies", "rules_details")
    op.drop_column("vacancies", "details_fetched_at")
    op.drop_column("vacancies", "key_skills")
    op.drop_column("vacancies", "work_format")
    op.drop_column("vacancies", "employment")
    op.drop_column("vacancies", "experience")
    op.drop_column("vacancies", "description")
