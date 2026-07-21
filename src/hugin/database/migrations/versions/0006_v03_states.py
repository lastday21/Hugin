"""Align vacancy and task states with specification 0.3."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_v03_states"
down_revision: str | Sequence[str] | None = "0005_vacancy_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATES = (
    "PENDING",
    "RUNNING",
    "RETRY_SCHEDULED",
    "REVIEW_REQUIRED",
    "INPUT_REQUIRED",
    "SKIPPED",
    "UNKNOWN_RESULT",
    "COMPLETED",
)
PREVIOUS_TASK_STATES = (
    "PENDING",
    "RUNNING",
    "RETRY_SCHEDULED",
    "SKIPPED",
    "UNKNOWN_RESULT",
    "COMPLETED",
)
VACANCY_STATES = (
    "DISCOVERED",
    "ANALYZED",
    "QUEUED",
    "FILTERED_OUT",
    "SKIPPED",
    "CLOSED",
)
PREVIOUS_VACANCY_STATES = (
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
    op.drop_constraint("task_state", "application_tasks", type_="check")
    op.create_check_constraint(
        "task_state",
        "application_tasks",
        f"state IN ({values(TASK_STATES)})",
    )

    op.drop_constraint("vacancy_state", "direction_vacancies", type_="check")
    op.execute(
        sa.text("UPDATE direction_vacancies SET state = 'ANALYZED' WHERE state = 'FILTERED'")
    )
    op.execute(
        sa.text("UPDATE direction_vacancies SET state = 'FILTERED_OUT' WHERE state = 'SKIPPED'")
    )
    op.create_check_constraint(
        "vacancy_state",
        "direction_vacancies",
        f"state IN ({values(VACANCY_STATES)})",
    )


def downgrade() -> None:
    op.drop_constraint("vacancy_state", "direction_vacancies", type_="check")
    op.execute(
        sa.text("UPDATE direction_vacancies SET state = 'SKIPPED' WHERE state = 'FILTERED_OUT'")
    )
    op.execute(
        sa.text("UPDATE direction_vacancies SET state = 'FILTERED' WHERE state = 'ANALYZED'")
    )
    op.create_check_constraint(
        "vacancy_state",
        "direction_vacancies",
        f"state IN ({values(PREVIOUS_VACANCY_STATES)})",
    )

    op.drop_constraint("task_state", "application_tasks", type_="check")
    op.execute(
        sa.text(
            "UPDATE application_tasks SET state = 'SKIPPED' "
            "WHERE state IN ('REVIEW_REQUIRED', 'INPUT_REQUIRED')"
        )
    )
    op.create_check_constraint(
        "task_state",
        "application_tasks",
        f"state IN ({values(PREVIOUS_TASK_STATES)})",
    )
