"""Add the complete persistent data model for specification 0.3."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_v03_data_model"
down_revision: str | Sequence[str] | None = "0006_v03_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCREENING_FORM_STATES = (
    "DRAFT",
    "REVIEW_REQUIRED",
    "INPUT_REQUIRED",
    "CONFIRMED",
    "INVALIDATED",
    "SENT",
)
RECRUITER_MESSAGE_STATES = (
    "RECEIVED",
    "DRAFT",
    "REVIEW_REQUIRED",
    "CONFIRMED",
    "SENT",
    "FAILED",
)


def values(items: tuple[str, ...]) -> str:
    return ", ".join(repr(item) for item in items)


def created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def updated_at() -> sa.Column[datetime]:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    op.add_column(
        "direction_search_queries",
        sa.Column(
            "regions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "direction_search_queries",
        sa.Column(
            "work_formats",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "direction_search_queries",
        sa.Column("schedule_minutes", sa.Integer(), nullable=False, server_default="120"),
    )
    op.add_column(
        "direction_search_queries",
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "direction_search_queries",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_direction_search_queries_schedule_minutes",
        "direction_search_queries",
        "schedule_minutes >= 5",
    )

    op.add_column("resumes", sa.Column("source_type", sa.String(length=32), nullable=True))
    op.add_column("resumes", sa.Column("source_reference", sa.Text(), nullable=True))
    op.add_column("resumes", sa.Column("content_text", sa.Text(), nullable=True))
    op.add_column(
        "resumes",
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "direction_resumes",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="PRIMARY"),
    )
    op.add_column(
        "direction_resumes",
        sa.Column(
            "separate_application_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "resume_mapping_role",
        "direction_resumes",
        f"role IN ({values(('PRIMARY', 'RESERVE'))})",
    )

    vacancy_columns: tuple[sa.Column[Any], ...] = (
        sa.Column("region", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("salary_from", sa.Numeric(14, 2), nullable=True),
        sa.Column("salary_to", sa.Numeric(14, 2), nullable=True),
        sa.Column("salary_currency", sa.String(length=8), nullable=True),
        sa.Column("salary_gross", sa.Boolean(), nullable=True),
        sa.Column("schedule", sa.String(length=255), nullable=True),
        sa.Column("responsibilities", sa.Text(), nullable=True),
        sa.Column("required_qualifications", sa.Text(), nullable=True),
        sa.Column("preferred_qualifications", sa.Text(), nullable=True),
        sa.Column("has_cover_letter", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_screening_form", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_external_link", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_test_assignment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    for column in vacancy_columns:
        op.add_column("vacancies", column)

    op.add_column(
        "direction_vacancies",
        sa.Column("rules_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "direction_vacancies",
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        created_at(),
        updated_at(),
        sa.ForeignKeyConstraint(["account_id"], ["hh_accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("account_id", name="uq_candidate_profiles_account_id"),
    )
    op.create_index("ix_candidate_profiles_account_id", "candidate_profiles", ["account_id"])

    op.create_table(
        "verified_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("actual_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_id", sa.Integer(), nullable=True),
        sa.Column("direction_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("allow_in_letters", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_in_forms", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_in_messages", sa.Boolean(), nullable=False, server_default=sa.false()),
        created_at(),
        updated_at(),
        sa.CheckConstraint(
            f"state IN ({values(('PENDING', 'CONFIRMED', 'REJECTED'))})",
            name="fact_confirmation_state",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_verified_facts_profile_id", "verified_facts", ["profile_id"])
    op.create_index("ix_verified_facts_resume_id", "verified_facts", ["resume_id"])
    op.create_index("ix_verified_facts_direction_id", "verified_facts", ["direction_id"])

    op.create_table(
        "company_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("direction_id", sa.Integer(), nullable=False),
        sa.Column("company_pattern", sa.String(length=255), nullable=False),
        sa.Column("rule_type", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        created_at(),
        sa.CheckConstraint(
            f"rule_type IN ({values(('ALLOW', 'BLOCK'))})",
            name="company_rule_type",
        ),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "direction_id",
            "company_pattern",
            "rule_type",
            name="uq_company_rules_direction_pattern_type",
        ),
    )
    op.create_index("ix_company_rules_direction_id", "company_rules", ["direction_id"])

    op.create_table(
        "vacancy_discoveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("direction_id", sa.Integer(), nullable=True),
        sa.Column("search_query_id", sa.Integer(), nullable=True),
        sa.Column("query_text", sa.String(length=512), nullable=False),
        sa.Column("region", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["vacancy_id"], ["vacancies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["search_query_id"], ["direction_search_queries.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "vacancy_id",
            "direction_id",
            "query_text",
            "region",
            name="uq_vacancy_discoveries_source",
        ),
    )
    op.create_index("ix_vacancy_discoveries_vacancy_id", "vacancy_discoveries", ["vacancy_id"])
    op.create_index("ix_vacancy_discoveries_direction_id", "vacancy_discoveries", ["direction_id"])
    op.create_index(
        "ix_vacancy_discoveries_search_query_id",
        "vacancy_discoveries",
        ["search_query_id"],
    )

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("instruction_text", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        created_at(),
        sa.UniqueConstraint("purpose", "version", name="uq_prompt_versions_purpose_version"),
    )

    op.create_table(
        "cover_letters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("direction_id", sa.Integer(), nullable=True),
        sa.Column("resume_id", sa.Integer(), nullable=False),
        sa.Column("prompt_version_id", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("instruction_version", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PENDING"),
        created_at(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"state IN ({values(('PENDING', 'READY', 'FAILED', 'SENT'))})",
            name="cover_letter_state",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vacancy_id"], ["vacancies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["direction_id"], ["career_directions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "application_id",
            "instruction_version",
            name="uq_cover_letters_application_instruction",
        ),
    )
    op.create_index("ix_cover_letters_application_id", "cover_letters", ["application_id"])
    op.create_index("ix_cover_letters_vacancy_id", "cover_letters", ["vacancy_id"])
    op.create_index("ix_cover_letters_direction_id", "cover_letters", ["direction_id"])
    op.create_index("ix_cover_letters_resume_id", "cover_letters", ["resume_id"])

    op.create_table(
        "cover_letter_facts",
        sa.Column("cover_letter_id", sa.Integer(), primary_key=True),
        sa.Column("fact_id", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(["cover_letter_id"], ["cover_letters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_id"], ["verified_facts.id"], ondelete="RESTRICT"),
    )

    op.create_table(
        "screening_forms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("version_hash", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="DRAFT"),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        created_at(),
        updated_at(),
        sa.CheckConstraint(
            f"state IN ({values(SCREENING_FORM_STATES)})",
            name="screening_form_state",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "application_id",
            "version_hash",
            name="uq_screening_forms_application_version",
        ),
    )
    op.create_index("ix_screening_forms_application_id", "screening_forms", ["application_id"])

    op.create_table(
        "screening_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("form_id", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.String(length=255), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("field_type", sa.String(length=64), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("max_length", sa.Integer(), nullable=True),
        sa.Column("format_hint", sa.String(length=255), nullable=True),
        sa.Column("has_attachment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_external_action", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_test_assignment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("position >= 0", name="ck_screening_questions_position"),
        sa.ForeignKeyConstraint(["form_id"], ["screening_forms.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("form_id", "field_key", name="uq_screening_questions_form_key"),
    )
    op.create_index("ix_screening_questions_form_id", "screening_questions", ["form_id"])

    op.create_table(
        "screening_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("verified_fact_id", sa.Integer(), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        created_at(),
        updated_at(),
        sa.CheckConstraint(
            f"source IS NULL OR source IN ({values(('PROFILE', 'BANK', 'YANDEXGPT', 'USER'))})",
            name="screening_answer_source",
        ),
        sa.ForeignKeyConstraint(["question_id"], ["screening_questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["verified_fact_id"], ["verified_facts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("question_id", name="uq_screening_answers_question_id"),
    )
    op.create_index("ix_screening_answers_question_id", "screening_answers", ["question_id"])

    op.create_table(
        "answer_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("question_pattern", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("verified_fact_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        created_at(),
        updated_at(),
        sa.ForeignKeyConstraint(["profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["verified_fact_id"], ["verified_facts.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_answer_templates_profile_id", "answer_templates", ["profile_id"])

    op.create_table(
        "recruiter_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("hh_id", sa.String(length=128), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        created_at(),
        sa.CheckConstraint(
            f"direction IN ({values(('INCOMING', 'OUTGOING'))})",
            name="message_direction",
        ),
        sa.CheckConstraint(
            f"state IN ({values(RECRUITER_MESSAGE_STATES)})",
            name="recruiter_message_state",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "application_id",
            "hh_id",
            name="uq_recruiter_messages_application_hh_id",
        ),
    )
    op.create_index(
        "ix_recruiter_messages_application_id", "recruiter_messages", ["application_id"]
    )

    op.create_table(
        "recruiter_message_facts",
        sa.Column("message_id", sa.Integer(), primary_key=True),
        sa.Column("fact_id", sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(["message_id"], ["recruiter_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_id"], ["verified_facts.id"], ondelete="RESTRICT"),
    )

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("hh_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("interview_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("booking_url", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="RECEIVED"),
        created_at(),
        updated_at(),
        sa.CheckConstraint(
            f"state IN ({values(('RECEIVED', 'PREPARING', 'SCHEDULED', 'CLOSED'))})",
            name="invitation_state",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("application_id", "hh_id", name="uq_invitations_application_hh_id"),
    )
    op.create_index("ix_invitations_application_id", "invitations", ["application_id"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("scope_type", sa.String(length=64), nullable=True),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("diagnostic_path", sa.Text(), nullable=True),
        sa.Column("snapshot_path", sa.Text(), nullable=True),
        sa.Column("trace_path", sa.Text(), nullable=True),
        created_at(),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"severity IN ({values(('INFO', 'WARNING', 'ERROR', 'CRITICAL'))})",
            name="incident_severity",
        ),
        sa.CheckConstraint(
            f"state IN ({values(('OPEN', 'RESOLVED'))})",
            name="incident_state",
        ),
    )
    op.create_index("ix_incidents_code", "incidents", ["code"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("incident_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        created_at(),
        sa.CheckConstraint(
            f"channel IN ({values(('WINDOWS', 'TELEGRAM', 'EMAIL'))})",
            name="notification_channel",
        ),
        sa.CheckConstraint(
            f"state IN ({values(('PENDING', 'SENT', 'FAILED'))})",
            name="notification_delivery_state",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_notifications_application_id", "notifications", ["application_id"])
    op.create_index("ix_notifications_incident_id", "notifications", ["incident_id"])

    op.create_table(
        "application_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timezone_name", sa.String(length=128), nullable=False),
        sa.Column("search_interval_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("message_interval_minutes", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("status_interval_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("hh_apply_daily_limit", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("hh_apply_delay_min_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("hh_apply_delay_max_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column(
            "windows_notifications_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("telegram_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "notification_routing",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("diagnostics_retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("logs_retention_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("backups_retention_days", sa.Integer(), nullable=False, server_default="30"),
        created_at(),
        updated_at(),
        sa.CheckConstraint("id = 1", name="ck_application_settings_singleton"),
        sa.CheckConstraint(
            "hh_apply_daily_limit >= 25", name="ck_application_settings_daily_limit"
        ),
        sa.CheckConstraint(
            "hh_apply_delay_min_seconds >= 0", name="ck_application_settings_delay_min"
        ),
        sa.CheckConstraint(
            "hh_apply_delay_max_seconds >= hh_apply_delay_min_seconds",
            name="ck_application_settings_delay_order",
        ),
        sa.CheckConstraint(
            "search_interval_minutes >= 5",
            name="ck_application_settings_search_interval",
        ),
        sa.CheckConstraint(
            "message_interval_minutes >= 1 AND status_interval_minutes >= 1",
            name="ck_application_settings_sync_intervals",
        ),
        sa.CheckConstraint(
            "diagnostics_retention_days >= 1 AND logs_retention_days >= 1 "
            "AND backups_retention_days >= 1",
            name="ck_application_settings_retention",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO application_settings (id, timezone_name) "
            "VALUES (1, current_setting('TIMEZONE'))"
        )
    )


def downgrade() -> None:
    op.drop_table("application_settings")
    op.drop_index("ix_notifications_incident_id", table_name="notifications")
    op.drop_index("ix_notifications_application_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_incidents_code", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_invitations_application_id", table_name="invitations")
    op.drop_table("invitations")
    op.drop_table("recruiter_message_facts")
    op.drop_index("ix_recruiter_messages_application_id", table_name="recruiter_messages")
    op.drop_table("recruiter_messages")
    op.drop_index("ix_answer_templates_profile_id", table_name="answer_templates")
    op.drop_table("answer_templates")
    op.drop_index("ix_screening_answers_question_id", table_name="screening_answers")
    op.drop_table("screening_answers")
    op.drop_index("ix_screening_questions_form_id", table_name="screening_questions")
    op.drop_table("screening_questions")
    op.drop_index("ix_screening_forms_application_id", table_name="screening_forms")
    op.drop_table("screening_forms")
    op.drop_table("cover_letter_facts")
    op.drop_index("ix_cover_letters_resume_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_direction_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_vacancy_id", table_name="cover_letters")
    op.drop_index("ix_cover_letters_application_id", table_name="cover_letters")
    op.drop_table("cover_letters")
    op.drop_table("prompt_versions")
    op.drop_index("ix_vacancy_discoveries_search_query_id", table_name="vacancy_discoveries")
    op.drop_index("ix_vacancy_discoveries_direction_id", table_name="vacancy_discoveries")
    op.drop_index("ix_vacancy_discoveries_vacancy_id", table_name="vacancy_discoveries")
    op.drop_table("vacancy_discoveries")
    op.drop_index("ix_company_rules_direction_id", table_name="company_rules")
    op.drop_table("company_rules")
    op.drop_index("ix_verified_facts_direction_id", table_name="verified_facts")
    op.drop_index("ix_verified_facts_resume_id", table_name="verified_facts")
    op.drop_index("ix_verified_facts_profile_id", table_name="verified_facts")
    op.drop_table("verified_facts")
    op.drop_index("ix_candidate_profiles_account_id", table_name="candidate_profiles")
    op.drop_table("candidate_profiles")

    op.drop_column("direction_vacancies", "analyzed_at")
    op.drop_column("direction_vacancies", "rules_version")

    for column_name in (
        "updated_at",
        "has_test_assignment",
        "has_external_link",
        "has_screening_form",
        "has_cover_letter",
        "preferred_qualifications",
        "required_qualifications",
        "responsibilities",
        "schedule",
        "salary_gross",
        "salary_currency",
        "salary_to",
        "salary_from",
        "address",
        "region",
    ):
        op.drop_column("vacancies", column_name)

    op.drop_constraint("resume_mapping_role", "direction_resumes", type_="check")
    op.drop_column("direction_resumes", "separate_application_allowed")
    op.drop_column("direction_resumes", "role")

    op.drop_column("resumes", "imported_at")
    op.drop_column("resumes", "content_text")
    op.drop_column("resumes", "source_reference")
    op.drop_column("resumes", "source_type")

    op.drop_constraint(
        "ck_direction_search_queries_schedule_minutes",
        "direction_search_queries",
        type_="check",
    )
    op.drop_column("direction_search_queries", "next_run_at")
    op.drop_column("direction_search_queries", "last_run_at")
    op.drop_column("direction_search_queries", "schedule_minutes")
    op.drop_column("direction_search_queries", "work_formats")
    op.drop_column("direction_search_queries", "regions")
