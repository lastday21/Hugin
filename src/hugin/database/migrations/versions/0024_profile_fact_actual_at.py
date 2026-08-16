"""Restore the confirmation time for legacy mutable profile facts."""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_profile_fact_actual_at"
down_revision: str | Sequence[str] | None = "0023_cover_letter_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE verified_facts AS fact
        SET actual_at = COALESCE(question.answered_at, question.updated_at, fact.updated_at)
        FROM profile_questions AS question
        WHERE fact.profile_id = question.profile_id
          AND fact.source_reference = 'profile-question:' || question.key
          AND fact.actual_at IS NULL
          AND fact.state = 'CONFIRMED'
          AND question.state = 'ANSWERED'
          AND fact.category IN (
              'available_from',
              'business_trips',
              'employment',
              'relocation',
              'salary_expectation',
              'work_format',
              'work_schedule'
          )
        """
    )


def downgrade() -> None:
    # The recovered time is profile data and remains valid after a code rollback.
    pass
