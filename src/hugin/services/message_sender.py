from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import ApplicationModel, IncidentModel, RecruiterMessageModel
from hugin.domain.content import IncidentState

UNCERTAIN_MESSAGE_SENDER = "HH_MESSAGE_SENDER_UNCERTAIN"


def uncertain_sender_message_ids(session: Session, *, account_id: int) -> frozenset[int]:
    return frozenset(
        session.scalars(
            select(RecruiterMessageModel.id)
            .join(ApplicationModel, ApplicationModel.id == RecruiterMessageModel.application_id)
            .join(IncidentModel, IncidentModel.scope_id == RecruiterMessageModel.id)
            .where(
                ApplicationModel.account_id == account_id,
                IncidentModel.scope_type == "recruiter_message",
                IncidentModel.code == UNCERTAIN_MESSAGE_SENDER,
                IncidentModel.state == IncidentState.OPEN,
            )
        )
    )


def has_uncertain_sender(session: Session, *, application_id: int) -> bool:
    return (
        session.scalar(
            select(IncidentModel.id)
            .join(RecruiterMessageModel, RecruiterMessageModel.id == IncidentModel.scope_id)
            .where(
                RecruiterMessageModel.application_id == application_id,
                IncidentModel.scope_type == "recruiter_message",
                IncidentModel.code == UNCERTAIN_MESSAGE_SENDER,
                IncidentModel.state == IncidentState.OPEN,
            )
            .limit(1)
        )
        is not None
    )
