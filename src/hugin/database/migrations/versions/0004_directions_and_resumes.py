"""Create scalable directions and resume mappings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_directions_and_resumes"
down_revision: str | Sequence[str] | None = "0003_queue_and_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPLICATION_STATES = (
    "APPLYING",
    "APPLIED",
    "VIEWED",
    "INVITED",
    "REJECTED",
    "CLOSED",
)
PREVIOUS_APPLICATION_STATES = (
    "DISCOVERED",
    "FILTERED",
    "ANALYZED",
    "QUEUED",
    *APPLICATION_STATES,
)
VACANCY_STATES = (
    "DISCOVERED",
    "FILTERED",
    "ANALYZED",
    "QUEUED",
    "SKIPPED",
    "CLOSED",
)


def values(items: tuple[str, ...]) -> str:
    return ", ".join(repr(item) for item in items)


def upgrade() -> None:
    op.create_table(
        "hh_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.UniqueConstraint("external_id", name="uq_hh_accounts_external_id"),
    )
    op.create_table(
        "career_directions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "scoring_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.ForeignKeyConstraint(["account_id"], ["hh_accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("account_id", "name", name="uq_career_directions_account_name"),
    )
    op.create_index("ix_career_directions_account_id", "career_directions", ["account_id"])
    op.create_table(
        "direction_search_queries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("direction_id", sa.Integer(), nullable=False),
        sa.Column("query", sa.String(length=512), nullable=False),
        sa.Column("area", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "filters",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "direction_id",
            "query",
            "area",
            name="uq_direction_search_queries_direction_query_area",
        ),
    )
    op.create_index(
        "ix_direction_search_queries_direction_id",
        "direction_search_queries",
        ["direction_id"],
    )
    op.create_table(
        "resumes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("hh_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.ForeignKeyConstraint(["account_id"], ["hh_accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("account_id", "hh_id", name="uq_resumes_account_hh_id"),
        sa.UniqueConstraint("account_id", "id", name="uq_resumes_account_id_id"),
    )
    op.create_index("ix_resumes_account_id", "resumes", ["account_id"])
    op.create_table(
        "direction_resumes",
        sa.Column("direction_id", sa.Integer(), primary_key=True),
        sa.Column("resume_id", sa.Integer(), primary_key=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("priority >= 0", name="ck_direction_resumes_priority"),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "direction_vacancies",
        sa.Column("direction_id", sa.Integer(), primary_key=True),
        sa.Column("vacancy_id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("rules_score", sa.Float(), nullable=True),
        sa.Column("ai_score", sa.Float(), nullable=True),
        sa.Column("fit_score", sa.Float(), nullable=True),
        sa.Column(
            "first_seen_at",
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
        sa.CheckConstraint(f"state IN ({values(VACANCY_STATES)})", name="vacancy_state"),
        sa.CheckConstraint(
            "rules_score IS NULL OR (rules_score >= 0 AND rules_score <= 100)",
            name="ck_direction_vacancies_rules_score",
        ),
        sa.CheckConstraint(
            "ai_score IS NULL OR (ai_score >= 0 AND ai_score <= 100)",
            name="ck_direction_vacancies_ai_score",
        ),
        sa.CheckConstraint(
            "fit_score IS NULL OR (fit_score >= 0 AND fit_score <= 100)",
            name="ck_direction_vacancies_fit_score",
        ),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vacancy_id"], ["vacancies.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_direction_vacancies_queue",
        "direction_vacancies",
        ["direction_id", "state", "fit_score"],
    )

    op.add_column("applications", sa.Column("account_id", sa.Integer(), nullable=True))
    op.add_column("applications", sa.Column("resume_id", sa.Integer(), nullable=True))
    op.add_column("applications", sa.Column("direction_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_applications_account_id",
        "applications",
        "hh_accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_applications_account_resume",
        "applications",
        "resumes",
        ["account_id", "resume_id"],
        ["account_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_applications_direction_id",
        "applications",
        "career_directions",
        ["direction_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        sa.text(
            "INSERT INTO hh_accounts (label, is_active, created_at, updated_at) "
            "SELECT 'Imported data', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "WHERE EXISTS (SELECT 1 FROM applications)"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO resumes (account_id, hh_id, title, is_active, created_at, updated_at) "
            "SELECT account.id, source.resume_hh_id, source.resume_hh_id, TRUE, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM (SELECT DISTINCT resume_hh_id FROM applications) AS source "
            "CROSS JOIN (SELECT id FROM hh_accounts WHERE label = 'Imported data' "
            "ORDER BY id LIMIT 1) AS account"
        )
    )
    op.execute(
        sa.text(
            "UPDATE applications AS application "
            "SET account_id = resume.account_id, resume_id = resume.id "
            "FROM resumes AS resume "
            "WHERE resume.hh_id = application.resume_hh_id"
        )
    )
    op.alter_column("applications", "account_id", nullable=False)
    op.alter_column("applications", "resume_id", nullable=False)
    op.drop_constraint("uq_applications_vacancy_id", "applications", type_="unique")
    op.create_unique_constraint(
        "uq_applications_account_vacancy_resume",
        "applications",
        ["account_id", "vacancy_id", "resume_id"],
    )
    op.create_index("ix_applications_vacancy_id", "applications", ["vacancy_id"])
    op.create_index("ix_applications_direction_id", "applications", ["direction_id"])
    op.drop_constraint("application_state", "applications", type_="check")
    op.execute(
        sa.text(
            "UPDATE applications SET state = 'APPLYING' "
            "WHERE state IN ('DISCOVERED', 'FILTERED', 'ANALYZED', 'QUEUED')"
        )
    )
    op.create_check_constraint(
        "application_state",
        "applications",
        f"state IN ({values(APPLICATION_STATES)})",
    )
    op.drop_column("applications", "resume_hh_id")


def downgrade() -> None:
    op.add_column("applications", sa.Column("resume_hh_id", sa.String(length=64), nullable=True))
    op.execute(
        sa.text(
            "UPDATE applications AS application SET resume_hh_id = resume.hh_id "
            "FROM resumes AS resume WHERE resume.id = application.resume_id"
        )
    )
    op.alter_column("applications", "resume_hh_id", nullable=False)
    op.drop_constraint("application_state", "applications", type_="check")
    op.create_check_constraint(
        "application_state",
        "applications",
        f"state IN ({values(PREVIOUS_APPLICATION_STATES)})",
    )
    op.execute(
        sa.text(
            "DELETE FROM applications AS newer USING applications AS older "
            "WHERE newer.vacancy_id = older.vacancy_id AND newer.id > older.id"
        )
    )
    op.drop_index("ix_applications_direction_id", table_name="applications")
    op.drop_index("ix_applications_vacancy_id", table_name="applications")
    op.drop_constraint("uq_applications_account_vacancy_resume", "applications", type_="unique")
    op.create_unique_constraint("uq_applications_vacancy_id", "applications", ["vacancy_id"])
    op.drop_constraint("fk_applications_direction_id", "applications", type_="foreignkey")
    op.drop_constraint("fk_applications_account_resume", "applications", type_="foreignkey")
    op.drop_constraint("fk_applications_account_id", "applications", type_="foreignkey")
    op.drop_column("applications", "direction_id")
    op.drop_column("applications", "resume_id")
    op.drop_column("applications", "account_id")

    op.drop_index("ix_direction_vacancies_queue", table_name="direction_vacancies")
    op.drop_table("direction_vacancies")
    op.drop_table("direction_resumes")
    op.drop_index("ix_resumes_account_id", table_name="resumes")
    op.drop_table("resumes")
    op.drop_index("ix_direction_search_queries_direction_id", table_name="direction_search_queries")
    op.drop_table("direction_search_queries")
    op.drop_index("ix_career_directions_account_id", table_name="career_directions")
    op.drop_table("career_directions")
    op.drop_table("hh_accounts")
