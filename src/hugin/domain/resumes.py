from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ResumeSourceType(StrEnum):
    PDF = "PDF"
    DOCX = "DOCX"


@dataclass(frozen=True, slots=True)
class ResumeDocument:
    source_path: Path
    original_name: str
    source_type: ResumeSourceType
    sha256: str
    size_bytes: int
    page_count: int | None
    text: str


@dataclass(frozen=True, slots=True)
class ResumeFactCandidate:
    category: str
    content: str
    source_reference: str


@dataclass(frozen=True, slots=True)
class ProfileQuestionCandidate:
    key: str
    question: str


@dataclass(frozen=True, slots=True)
class ParsedResumeProfile:
    display_name: str | None
    title: str
    facts: tuple[ResumeFactCandidate, ...]
    missing_questions: tuple[ProfileQuestionCandidate, ...]


@dataclass(frozen=True, slots=True)
class ResumeImportResult:
    resume_id: int
    title: str
    stored_path: Path
    source_sha256: str
    facts_pending: int
    questions_pending: tuple[ProfileQuestionCandidate, ...]
    unchanged: bool


@dataclass(frozen=True, slots=True)
class ProfileFactReview:
    id: int
    category: str
    content: str
