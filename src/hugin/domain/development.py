from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class QualityLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DevelopmentItemKind(StrEnum):
    PROBLEM = "PROBLEM"
    TASK = "TASK"
    HYPOTHESIS = "HYPOTHESIS"
    MEASUREMENT = "MEASUREMENT"
    CHECK = "CHECK"
    IMPROVEMENT = "IMPROVEMENT"


class DevelopmentItemStatus(StrEnum):
    IDEA = "IDEA"
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    REJECTED = "REJECTED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"


class DevelopmentPriority(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class DevelopmentAssessmentRecord:
    id: int
    direction_key: str
    score: float
    confidence: QualityLevel
    evidence: str
    next_step: str
    author: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DevelopmentDirectionRecord:
    key: str
    block_key: str
    block_name: str
    block_position: int
    name: str
    position: int
    rule: str
    metric: str
    criticality: QualityLevel
    current_assessment: DevelopmentAssessmentRecord
    assessment_count: int


@dataclass(frozen=True, slots=True)
class DevelopmentItemRecord:
    id: int
    external_key: str | None
    kind: DevelopmentItemKind
    title: str
    direction_key: str
    status: DevelopmentItemStatus
    priority: DevelopmentPriority
    expected_metric: str
    evidence: str
    verification_method: str
    next_step: str
    actual_result: str
    reference_codes: tuple[str, ...]
    author: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DevelopmentBlockRecord:
    key: str
    name: str
    position: int
    score: float
    confidence: QualityLevel
    bottleneck_key: str
    bottleneck_name: str
    directions: tuple[DevelopmentDirectionRecord, ...]


@dataclass(frozen=True, slots=True)
class DevelopmentSnapshot:
    blocks: tuple[DevelopmentBlockRecord, ...]
    items: tuple[DevelopmentItemRecord, ...]
    assessments: tuple[DevelopmentAssessmentRecord, ...]
