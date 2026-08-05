"""Keep only the latest confirmed profile fact in each scope."""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_profile_fact_versions"
down_revision: str | Sequence[str] | None = "0019_cover_letter_rejections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY profile_id, category, direction_id
                    ORDER BY id DESC
                ) AS position
            FROM verified_facts
            WHERE state = 'CONFIRMED'
        )
        UPDATE verified_facts AS fact
        SET
            state = 'REJECTED',
            allow_in_letters = FALSE,
            allow_in_forms = FALSE,
            allow_in_messages = FALSE,
            updated_at = CURRENT_TIMESTAMP
        FROM ranked
        WHERE fact.id = ranked.id
          AND ranked.position > 1
        """
    )


def downgrade() -> None:
    # Исторические разрешения нельзя достоверно восстановить после очистки повторов.
    pass
