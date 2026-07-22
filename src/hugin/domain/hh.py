from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class HhResumeData:
    hh_id: str
    title: str

    def __post_init__(self) -> None:
        if not self.hh_id or len(self.hh_id) > 64:
            raise ValueError("Некорректный идентификатор резюме hh.ru")
        if not self.title or len(self.title) > 255:
            raise ValueError("Некорректное название резюме hh.ru")


@dataclass(frozen=True, slots=True)
class HhProfileData:
    external_id: str
    label: str
    resumes: tuple[HhResumeData, ...]

    def __post_init__(self) -> None:
        if not self.external_id or len(self.external_id) > 128:
            raise ValueError("Некорректный идентификатор аккаунта hh.ru")
        if not self.label or len(self.label) > 255:
            raise ValueError("Некорректное имя аккаунта hh.ru")
        resume_ids = [resume.hh_id for resume in self.resumes]
        if len(resume_ids) != len(set(resume_ids)):
            raise ValueError("hh.ru вернул повторяющиеся резюме")


@dataclass(frozen=True, slots=True)
class HhResumeDetails:
    hh_id: str
    title: str
    experience: str
    skills: str
    education: str


class HhApplyStatus(StrEnum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    QUESTIONS_REQUIRED = "QUESTIONS_REQUIRED"
    VACANCY_CLOSED = "VACANCY_CLOSED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    ACCOUNT_WARNING = "ACCOUNT_WARNING"
    RESUME_MISMATCH = "RESUME_MISMATCH"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"


@dataclass(frozen=True, slots=True)
class HhApplyResult:
    status: HhApplyStatus
    final_url: str
    confirmation: str = ""
    questions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    retry_after_seconds: int | None = None
