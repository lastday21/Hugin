from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ApplicationOutcome:
    revision: int = 0
    interview_at: datetime | None = None
    interview_evidence: str = ""
    rejection_reason: str = ""
    rejection_evidence: str = ""
    recorded_at: datetime | None = None


class StaleOutcomeError(ValueError):
    pass
