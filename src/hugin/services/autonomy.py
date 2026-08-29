# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from hugin.database.models import ApplicationSettingsModel
from hugin.domain.directions import ConfigPayload

MAX_REPLY_TEMPLATES = 50
MAX_TEMPLATE_KEY_LENGTH = 64
MAX_TEMPLATE_INCOMING_LENGTH = 2_000
MAX_TEMPLATE_RESPONSE_LENGTH = 5_000
DEFAULT_MUTABLE_FACT_VALIDITY_DAYS = 30
MIN_MUTABLE_FACT_VALIDITY_DAYS = 1
MAX_MUTABLE_FACT_VALIDITY_DAYS = 365

DEFAULT_AUTONOMY_POLICY: ConfigPayload = {
    "auto_apply_stretch": True,
    "auto_submit_simple_forms": True,
    "auto_prepare_replies": True,
    "auto_send_approved_replies": True,
    "auto_reconcile_unknown": True,
    "reuse_confirmed_profile_facts": True,
    "mark_opened_invitations_seen": True,
    "mutable_fact_validity_days": DEFAULT_MUTABLE_FACT_VALIDITY_DAYS,
    "reply_templates": [],
}


@dataclass(frozen=True, slots=True)
class ApprovedReplyTemplate:
    key: str
    incoming_text: str
    response_text: str
    enabled: bool

    @property
    def normalized_incoming_text(self) -> str:
        return normalize_message(self.incoming_text)


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    revision: int
    auto_apply_stretch: bool
    auto_submit_simple_forms: bool
    auto_prepare_replies: bool
    auto_send_approved_replies: bool
    auto_reconcile_unknown: bool
    reuse_confirmed_profile_facts: bool
    mark_opened_invitations_seen: bool
    mutable_fact_validity_days: int
    reply_templates: tuple[ApprovedReplyTemplate, ...]

    def matching_reply_template(self, incoming_text: str) -> ApprovedReplyTemplate | None:
        normalized = normalize_message(incoming_text)
        if not normalized:
            return None
        for template in self.reply_templates:
            if template.enabled and template.normalized_incoming_text == normalized:
                return template
        return None

    def as_payload(self) -> ConfigPayload:
        return {
            "auto_apply_stretch": self.auto_apply_stretch,
            "auto_submit_simple_forms": self.auto_submit_simple_forms,
            "auto_prepare_replies": self.auto_prepare_replies,
            "auto_send_approved_replies": self.auto_send_approved_replies,
            "auto_reconcile_unknown": self.auto_reconcile_unknown,
            "reuse_confirmed_profile_facts": self.reuse_confirmed_profile_facts,
            "mark_opened_invitations_seen": self.mark_opened_invitations_seen,
            "mutable_fact_validity_days": self.mutable_fact_validity_days,
            "reply_templates": [
                {
                    "key": template.key,
                    "incoming_text": template.incoming_text,
                    "response_text": template.response_text,
                    "enabled": template.enabled,
                }
                for template in self.reply_templates
            ],
        }


class AutonomyPolicyService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> AutonomyPolicy:
        settings = self._settings()
        return parse_autonomy_policy(
            settings.autonomy_policy,
            revision=settings.autonomy_policy_version,
        )

    def get_for_update(self) -> AutonomyPolicy:
        settings = self._settings(for_update=True)
        return parse_autonomy_policy(
            settings.autonomy_policy,
            revision=settings.autonomy_policy_version,
        )

    def update(self, payload: ConfigPayload) -> AutonomyPolicy:
        settings = self._settings(for_update=True)
        validated = parse_autonomy_policy(
            payload,
            revision=settings.autonomy_policy_version + 1,
        )
        settings.autonomy_policy = validated.as_payload()
        settings.autonomy_policy_version = validated.revision
        self._session.flush()
        return validated

    def _settings(self, *, for_update: bool = False) -> ApplicationSettingsModel:
        settings = (
            self._session.scalar(
                select(ApplicationSettingsModel)
                .where(ApplicationSettingsModel.id == 1)
                .with_for_update()
            )
            if for_update
            else self._session.get(ApplicationSettingsModel, 1)
        )
        if settings is None:
            raise LookupError("Настройки автономности не найдены")
        return settings


def parse_autonomy_policy(
    payload: ConfigPayload | None,
    *,
    revision: int,
) -> AutonomyPolicy:
    if revision < 1:
        raise ValueError("Версия политики автономности должна быть положительной")
    values = {**DEFAULT_AUTONOMY_POLICY, **dict(payload or {})}
    templates = _reply_templates(values.get("reply_templates"))
    return AutonomyPolicy(
        revision=revision,
        auto_apply_stretch=_boolean(values, "auto_apply_stretch"),
        auto_submit_simple_forms=_boolean(values, "auto_submit_simple_forms"),
        auto_prepare_replies=_boolean(values, "auto_prepare_replies"),
        auto_send_approved_replies=_boolean(values, "auto_send_approved_replies"),
        auto_reconcile_unknown=_boolean(values, "auto_reconcile_unknown"),
        reuse_confirmed_profile_facts=_boolean(values, "reuse_confirmed_profile_facts"),
        mark_opened_invitations_seen=_boolean(values, "mark_opened_invitations_seen"),
        mutable_fact_validity_days=_integer(
            values,
            "mutable_fact_validity_days",
            minimum=MIN_MUTABLE_FACT_VALIDITY_DAYS,
            maximum=MAX_MUTABLE_FACT_VALIDITY_DAYS,
        ),
        reply_templates=templates,
    )


def normalize_message(value: str) -> str:
    return " ".join(value.casefold().replace("\u00a0", " ").split())


def _boolean(values: ConfigPayload, key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Настройка «{key}» должна быть логическим значением")
    return value


def _integer(
    values: ConfigPayload,
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Настройка «{key}» должна быть целым числом")
    if not minimum <= value <= maximum:
        raise ValueError(f"Настройка «{key}» должна быть от {minimum} до {maximum}")
    return value


def _reply_templates(value: object) -> tuple[ApprovedReplyTemplate, ...]:
    if not isinstance(value, list):
        raise ValueError("Список утверждённых ответов имеет неверный формат")
    if len(value) > MAX_REPLY_TEMPLATES:
        raise ValueError(f"Разрешено не более {MAX_REPLY_TEMPLATES} утверждённых ответов")
    templates: list[ApprovedReplyTemplate] = []
    used_keys: set[str] = set()
    used_questions: set[str] = set()
    for position, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Утверждённый ответ №{position} имеет неверный формат")
        key = _required_text(raw.get("key"), "ключ", MAX_TEMPLATE_KEY_LENGTH)
        incoming_text = _required_text(
            raw.get("incoming_text"),
            "текст работодателя",
            MAX_TEMPLATE_INCOMING_LENGTH,
        )
        response_text = _required_text(
            raw.get("response_text"),
            "ответ",
            MAX_TEMPLATE_RESPONSE_LENGTH,
        )
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"Признак активности ответа «{key}» имеет неверный формат")
        normalized_key = key.casefold()
        normalized_question = normalize_message(incoming_text)
        if normalized_key in used_keys:
            raise ValueError(f"Ключ ответа «{key}» повторяется")
        if normalized_question in used_questions:
            raise ValueError("Один текст работодателя нельзя связать с несколькими ответами")
        used_keys.add(normalized_key)
        used_questions.add(normalized_question)
        templates.append(
            ApprovedReplyTemplate(
                key=key,
                incoming_text=incoming_text,
                response_text=response_text,
                enabled=enabled,
            )
        )
    return tuple(templates)


def _required_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Поле «{label}» должно быть строкой")
    selected = value.strip()
    if not selected:
        raise ValueError(f"Поле «{label}» не может быть пустым")
    if len(selected) > maximum:
        raise ValueError(f"Поле «{label}» длиннее {maximum} символов")
    return selected
