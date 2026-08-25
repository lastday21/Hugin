from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import IncidentModel
from hugin.domain.content import IncidentSeverity, IncidentState


class IncidentService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def report(
        self,
        *,
        code: str,
        severity: IncidentSeverity,
        message: str,
        scope_type: str,
        scope_id: int,
    ) -> IncidentModel:
        incident = self._session.scalar(
            select(IncidentModel).where(
                IncidentModel.code == code,
                IncidentModel.scope_type == scope_type,
                IncidentModel.scope_id == scope_id,
                IncidentModel.state == IncidentState.OPEN,
            )
        )
        if incident is None:
            incident = IncidentModel(
                code=code,
                severity=severity,
                state=IncidentState.OPEN,
                scope_type=scope_type,
                scope_id=scope_id,
                message=message[:500],
            )
            self._session.add(incident)
        else:
            incident.severity = severity
            incident.message = message[:500]
        self._session.flush()
        return incident

    def resolve(self, *, code: str, scope_type: str, scope_id: int) -> int:
        incidents = tuple(
            self._session.scalars(
                select(IncidentModel).where(
                    IncidentModel.code == code,
                    IncidentModel.scope_type == scope_type,
                    IncidentModel.scope_id == scope_id,
                    IncidentModel.state == IncidentState.OPEN,
                )
            )
        )
        resolved_at = datetime.now(UTC)
        for incident in incidents:
            incident.state = IncidentState.RESOLVED
            incident.resolved_at = resolved_at
        if incidents:
            self._session.flush()
        return len(incidents)
