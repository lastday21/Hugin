from __future__ import annotations

import hashlib
import json
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
class HhResumeExperienceBlock:
    company: str = ""
    position: str = ""
    period: str = ""
    description: str = ""
    text: str = ""


@dataclass(frozen=True, slots=True)
class HhResumeDetails:
    hh_id: str
    title: str
    experience: str
    skills: str
    education: str
    city: str = ""
    salary: str = ""
    employment: str = ""
    work_format: str = ""
    relocation: str = ""
    business_trips: str = ""
    about: str = ""
    experience_blocks: tuple[HhResumeExperienceBlock, ...] = ()


class HhApplyStatus(StrEnum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    QUESTIONS_REQUIRED = "QUESTIONS_REQUIRED"
    VACANCY_CLOSED = "VACANCY_CLOSED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    ACCOUNT_WARNING = "ACCOUNT_WARNING"
    RESUME_MISMATCH = "RESUME_MISMATCH"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"


@dataclass(frozen=True, slots=True)
class HhScreeningField:
    key: str
    question: str
    field_type: str
    is_required: bool = False
    options: tuple[str, ...] = ()
    max_length: int | None = None
    format_hint: str = ""
    has_attachment: bool = False
    has_external_action: bool = False
    has_test_assignment: bool = False

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > 255:
            raise ValueError("Некорректный ключ вопроса работодателя")
        if not self.question:
            raise ValueError("Текст вопроса работодателя отсутствует")
        if not self.field_type or len(self.field_type) > 64:
            raise ValueError("Некорректный тип поля работодателя")
        if self.max_length is not None and self.max_length < 1:
            raise ValueError("Ограничение длины ответа должно быть положительным")


@dataclass(frozen=True, slots=True)
class HhScreeningForm:
    fields: tuple[HhScreeningField, ...]
    warnings: tuple[str, ...] = ()


def screening_form_hash(form: HhScreeningForm) -> str:
    payload = {
        "fields": [
            {
                "key": field.key,
                "question": " ".join(field.question.split()),
                "type": field.field_type,
                "required": field.is_required,
                "options": list(field.options),
                "max_length": field.max_length,
                "format_hint": field.format_hint,
                "has_attachment": field.has_attachment,
                "has_external_action": field.has_external_action,
                "has_test_assignment": field.has_test_assignment,
            }
            for field in form.fields
        ],
        "warnings": [" ".join(warning.split()) for warning in form.warnings],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HhScreeningSubmission:
    version_hash: str
    answers: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if len(self.version_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.version_hash.casefold()
        ):
            raise ValueError("Некорректный отпечаток анкеты")
        keys = [key for key, _answer in self.answers]
        if any(
            not key or len(key) > 255 or not answer.strip() or len(answer) > 4000
            for key, answer in self.answers
        ):
            raise ValueError("Ответы анкеты должны содержать ключ и значение")
        if len(keys) != len(set(keys)):
            raise ValueError("Ответы анкеты содержат повторяющиеся поля")

    @property
    def answer_map(self) -> dict[str, str]:
        return dict(self.answers)


class HhFormReviewStatus(StrEnum):
    READY = "READY"
    FORM_CHANGED = "FORM_CHANGED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    VACANCY_CLOSED = "VACANCY_CLOSED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    RESUME_MISMATCH = "RESUME_MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class HhFormReviewResult:
    status: HhFormReviewStatus
    final_url: str
    current_form: HhScreeningForm | None = None
    filled_keys: tuple[str, ...] = ()
    skipped_keys: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True, slots=True)
class HhApplyResult:
    status: HhApplyStatus
    final_url: str
    confirmation: str = ""
    questions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    retry_after_seconds: int | None = None
    screening_form: HhScreeningForm | None = None
    screening_form_version_hash: str | None = None
