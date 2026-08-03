"""Prevent historical events from flooding newly enabled channels."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_notification_cutoffs"
down_revision: str | Sequence[str] | None = "0017_supervised_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_settings",
        sa.Column(
            "notification_cutoffs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE application_settings AS settings
            SET notification_cutoffs = COALESCE(
                (
                    SELECT jsonb_object_agg(
                        route.key || ':' || channel.value,
                        to_jsonb(settings.updated_at)
                    )
                    FROM jsonb_each(settings.notification_routing) AS route
                    CROSS JOIN LATERAL jsonb_array_elements_text(route.value) AS channel(value)
                    WHERE channel.value IN ('WINDOWS', 'TELEGRAM', 'EMAIL')
                ),
                '{}'::jsonb
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE notifications
            SET state = 'FAILED', error_code = 'HISTORICAL_EVENT_SUPPRESSED'
            WHERE state = 'PENDING' AND channel IN ('TELEGRAM', 'EMAIL')
            """
        )
    )


def downgrade() -> None:
    op.drop_column("application_settings", "notification_cutoffs")
