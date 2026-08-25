# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hugin.database.models import ApplicationSettingsModel
from hugin.domain.directions import ConfigPayload

MAX_PROMPT_LENGTH = 4000
ALICE_AI_MODEL = "aliceai-llm/latest"
QWEN3_AI_MODEL = "qwen3-235b-a22b-fp8/latest"
DEFAULT_AI_MODEL = ALICE_AI_MODEL
DEFAULT_REASONING_EFFORT = "high"


@dataclass(frozen=True, slots=True)
class AiModelOption:
    value: str
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class AiReasoningOption:
    value: str
    title: str
    description: str


AI_MODEL_OPTIONS = (
    AiModelOption(
        value=ALICE_AI_MODEL,
        title="Alice AI",
        description="Лучше подходит для естественных писем и диалогов.",
    ),
    AiModelOption(
        value=QWEN3_AI_MODEL,
        title="Qwen 3 235B",
        description="Мощная альтернативная модель для сложных текстов.",
    ),
)
AI_MODEL_VALUES = frozenset(option.value for option in AI_MODEL_OPTIONS)
AI_REASONING_OPTIONS = (
    AiReasoningOption(
        value="low",
        title="Быстрый",
        description="Ответ быстрее и дешевле.",
    ),
    AiReasoningOption(
        value="medium",
        title="Сбалансированный",
        description="Баланс скорости и качества.",
    ),
    AiReasoningOption(
        value="high",
        title="Глубокий",
        description="Приоритет качества и тщательной проверки.",
    ),
)
AI_REASONING_VALUES = frozenset(option.value for option in AI_REASONING_OPTIONS)

DEFAULT_RESUME_PROMPT = (
    "Пиши кратко и предметно, используй активные формулировки. "
    "Сохраняй смысл и факты исходного резюме, делай текст подходящим для выбранной роли."
)
DEFAULT_COVER_LETTER_PROMPT = (
    "Пиши 2–3 коротких абзаца общим объёмом 500–900 знаков. "
    "Сразу после «Здравствуйте!» покажи самый сильный подтверждённый пример под основную "
    "задачу вакансии: что кандидат делал сам, с чем работал и какой результат получил. "
    "Второй пример добавляй только для другого важного требования вакансии. "
    "Не повторяй название вакансии и компании, не пересказывай требования и не оценивай "
    "кандидата общими словами. Не используй вводные «меня заинтересовала вакансия», "
    "«вижу, что», «уверен, что», «этот опыт пригодится» и «быстро включусь». "
    "Для серверной разработки начинай с серверного опыта; для данных — с обработки и проверки "
    "данных; для ИИ и интеграций — с соответствующего прототипа. Для совмещённой серверной "
    "и интерфейсной роли выбирай пример, где обе части сделаны в одном проекте. Руководство "
    "разработкой, планирование и проверку изменений упоминай только там, где важны "
    "самостоятельность, "
    "планирование или проверка кода. Если в подтверждённых фактах нет обязательной технологии, "
    "не упоминай её и не извиняйся, если работодатель не просит ответить об этом прямо. "
    "Личный проект используй только тогда, когда он ближе к задачам вакансии или подтверждает "
    "отдельное важное требование; называй его проектом, а не местом работы или коммерческим "
    "опытом. "
    "Последнее предложение связывай с одной конкретной задачей вакансии, без общей просьбы "
    "обсудить опыт."
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

    def get_model(self) -> str:
        stored = self._settings().ai_prompt_overrides
        value = stored.get("model")
        if isinstance(value, str):
            selected = value.strip()
            if selected in AI_MODEL_VALUES:
                return selected
        return DEFAULT_AI_MODEL

    def get_reasoning_effort(self) -> str:
        stored = self._settings().ai_prompt_overrides
        value = stored.get("reasoning_effort")
        if isinstance(value, str):
            selected = value.strip()
            if selected in AI_REASONING_VALUES:
                return selected
        return DEFAULT_REASONING_EFFORT

    def update_model(self, model: str, reasoning_effort: str | None = None) -> str:
        selected = model.strip()
        if selected not in AI_MODEL_VALUES:
            raise ValueError("Выбрана недоступная модель")
        settings = self._settings()
        values = dict(settings.ai_prompt_overrides)
        values["model"] = selected
        if reasoning_effort is not None:
            effort = reasoning_effort.strip()
            if effort not in AI_REASONING_VALUES:
                raise ValueError("Выбран недоступный режим обработки")
            values["reasoning_effort"] = effort
        settings.ai_prompt_overrides = values
        self._session.flush()
        return selected

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
        values: ConfigPayload = dict(settings.ai_prompt_overrides)
        values.update(
            {
                "resume": selected.resume,
                "cover_letter": selected.cover_letter,
                "recruiter_reply": selected.recruiter_reply,
            }
        )
        settings.ai_prompt_overrides = values
        self._session.flush()
        return selected

    def reset(self) -> AiPromptSettings:
        settings = self._settings()
        values = dict(settings.ai_prompt_overrides)
        for key in ("resume", "cover_letter", "recruiter_reply"):
            values.pop(key, None)
        settings.ai_prompt_overrides = values
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
