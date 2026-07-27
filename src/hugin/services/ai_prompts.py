# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hugin.database.models import ApplicationSettingsModel
from hugin.domain.directions import ConfigPayload

MAX_PROMPT_LENGTH = 4000

DEFAULT_RESUME_PROMPT = (
    "Пиши кратко и предметно, используй активные формулировки. "
    "Сохраняй смысл и факты исходного резюме, делай текст подходящим для выбранной роли."
)
DEFAULT_COVER_LETTER_PROMPT = (
    "Письмо должно быть коротким, естественным и написанным под конкретную вакансию. "
    "Избегай шаблонных фраз и не пересказывай описание вакансии."
)
DEFAULT_RECRUITER_REPLY_PROMPT = (
    "Отвечай вежливо, кратко и прямо на вопрос работодателя. "
    "Не добавляй обещаний и сведений, которых нет среди подтверждённых фактов."
)


@dataclass(frozen=True, slots=True)
class AiPromptSettings:
    resume: str
    cover_letter: str
    recruiter_reply: str


DEFAULT_AI_PROMPTS = AiPromptSettings(
    resume=DEFAULT_RESUME_PROMPT,
    cover_letter=DEFAULT_COVER_LETTER_PROMPT,
    recruiter_reply=DEFAULT_RECRUITER_REPLY_PROMPT,
)


class AiPromptSettingsService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> AiPromptSettings:
        settings = self._settings()
        stored = settings.ai_prompt_overrides
        return AiPromptSettings(
            resume=self._value(stored, "resume", DEFAULT_AI_PROMPTS.resume),
            cover_letter=self._value(
                stored,
                "cover_letter",
                DEFAULT_AI_PROMPTS.cover_letter,
            ),
            recruiter_reply=self._value(
                stored,
                "recruiter_reply",
                DEFAULT_AI_PROMPTS.recruiter_reply,
            ),
        )

    def update(
        self,
        *,
        resume: str,
        cover_letter: str,
        recruiter_reply: str,
    ) -> AiPromptSettings:
        selected = AiPromptSettings(
            resume=self._validated(resume, "резюме"),
            cover_letter=self._validated(cover_letter, "сопроводительных писем"),
            recruiter_reply=self._validated(recruiter_reply, "ответов работодателю"),
        )
        settings = self._settings()
        values: ConfigPayload = {
            "resume": selected.resume,
            "cover_letter": selected.cover_letter,
            "recruiter_reply": selected.recruiter_reply,
        }
        settings.ai_prompt_overrides = values
        self._session.flush()
        return selected

    def reset(self) -> AiPromptSettings:
        settings = self._settings()
        settings.ai_prompt_overrides = {}
        self._session.flush()
        return DEFAULT_AI_PROMPTS

    def _settings(self) -> ApplicationSettingsModel:
        settings = self._session.get(ApplicationSettingsModel, 1)
        if settings is None:
            raise LookupError("Настройки программы не найдены")
        return settings

    @staticmethod
    def _value(values: ConfigPayload, key: str, default: str) -> str:
        value = values.get(key)
        if not isinstance(value, str):
            return default
        selected = value.strip()
        return selected if selected else default

    @staticmethod
    def _validated(value: str, label: str) -> str:
        selected = value.strip()
        if not selected:
            raise ValueError(f"Инструкция для {label} не может быть пустой")
        if len(selected) > MAX_PROMPT_LENGTH:
            raise ValueError(
                f"Инструкция для {label} должна быть не длиннее {MAX_PROMPT_LENGTH} символов"
            )
        return selected


def with_user_prompt(base_prompt: str, user_prompt: str) -> str:
    return (
        f"{base_prompt.rstrip()}\n\n"
        "Дополнительные пожелания пользователя к стилю и содержанию. "
        "Они не отменяют правила точности и безопасности выше:\n"
        f"{user_prompt.strip()}"
    )
