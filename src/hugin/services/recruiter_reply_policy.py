# ruff: noqa: RUF001

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from hugin.domain.applications import ApplicationState
from hugin.domain.content import MessageDirection, RecruiterActionKind, RecruiterMessageState
from hugin.services.autonomy import normalize_message


class RecruiterReplyDisposition(StrEnum):
    NO_REPLY = "NO_REPLY"
    AUTOMATIC_DRAFT = "AUTOMATIC_DRAFT"
    REVIEW_DRAFT = "REVIEW_DRAFT"
    MANUAL = "MANUAL"
    AMBIGUOUS = "AMBIGUOUS"


class RecruiterReplyHistoryMessage(Protocol):
    @property
    def body(self) -> str: ...

    @property
    def direction(self) -> MessageDirection: ...

    @property
    def state(self) -> RecruiterMessageState: ...


_TERMINAL_APPLICATION_STATES = {
    ApplicationState.REJECTED,
    ApplicationState.CLOSED,
}

_HH_INVITATION_REMINDER_PATTERN = re.compile(
    r"\b(?:(?:ответьте|ответить)\b.{0,100}\bна приглашени|"
    r"напомина\w*\b.{0,120}\bприглашени)",
    re.I | re.S,
)

_NO_REPLY_PATTERNS = tuple(
    re.compile(pattern, re.I | re.S)
    for pattern in (
        r"\bк сожалению\b.{0,250}\b(?:не готовы|не можем|не сможем|не пригласим|отказ)",
        r"\b(?:не готовы|не можем|не сможем)\b.{0,180}\b"
        r"(?:пригласить|предложить|продолжить|рассматривать)",
        r"\b(?:приняли решение|остановили выбор|остановились)\b.{0,180}\b"
        r"(?:друг\w* кандидат|не продолжать)",
        r"\b(?:выбрали|нашли)\b.{0,120}\bдруг\w* кандидат",
        r"\bкандидатур\w*\b.{0,120}\b(?:не подход|отклон)",
        r"\b(?:резюме|отклик|кандидатур\w*)\b.{0,100}\b(?:отклон|не прош)",
        r"\bваканси\w*\b.{0,100}\b(?:закрыт|закрыва|уже не актуальн)",
        r"\b(?:рассмотрим|изучим)\b.{0,100}\b(?:резюме|отклик)",
        r"\b(?:компани\w*|команд\w*|работодател\w*)\b.{0,80}\b"
        r"(?:рассмотрит|рассмотрят|изучит|изучат)\b.{0,100}\b(?:резюме|отклик|кандидатур)",
        r"\bесли\b.{0,150}\b(?:подойд|заинтерес)\w*\b.{0,150}\b(?:свяж|напиш|позвон)",
        r"\b(?:в случае|при)\b.{0,100}\bсоответств\w*\b.{0,150}\b"
        r"(?:свяж|напиш|позвон)",
        r"\bпредставител\w* работодател\w*\b.{0,150}\b(?:ознаком|свяж)",
        r"\b(?:опрос|анкет\w*)\b.{0,100}\b(?:заверш|пройден|получен)",
        r"\bблагодар\w*\b.{0,120}\b(?:за ответы|за прохождение|ответы получены)",
        r"\bспасибо\b.{0,140}\b(?:за\s+(?:ответ\w*|удел[её]нн\w*\s+(?:нам\s+)?врем\w*)|"
        r"что\s+(?:ответил\w*|уделил\w*))",
        r"\b(?:спасибо|благодар\w*)\b.{0,180}\b(?:успехов|удачи)\b",
        r"\b(?:продолжаем|будем)\b.{0,120}\b(?:рассматрив|изучать)\w*\b.{0,180}\b"
        r"(?:резюме|отклик|кандидат)",
        r"\bпродолжаем\s+рассмотрение\b.{0,120}\bкандидат\w*\b.{0,180}\b"
        r"(?:потребуется|нужно)\b.{0,80}\bврем\w*",
        r"\b(?:верн[её]мся|свяжемся|сообщим|уведомим)\b.{0,160}\b"
        r"(?:с\s+обратн\w*\s+связ\w*|о\s+(?:решени|результат|статус)|"
        r"следующ\w*\s+этап)",
        r"\b(?:вс[её]\s+)?(?:зафиксировал|записал)\w*\b.{0,100}\bспасибо\b",
        r"\bрезюме\b.{0,140}\bнаходится\b.{0,100}\bэтап\w*\b.{0,80}\bрассмотрен",
        r"\bна данный момент\b.{0,180}\bрешил\w*\b.{0,120}\bрассматриват\w*\b"
        r".{0,180}\bкандидат",
        r"\b(?:подробн\w*\s+(?:описани|информац)\w*|информац\w*\s+о\s+компани\w*)\b",
        r"\b(?:ии|ai)[ -]?(?:помощник|ассистент)\b.{0,120}\bзавершил\w*\s+работ",
        r"\bберу\s+время\b.{0,180}\b(?:изучен|разбор|рассмотрен)\w*\b",
        r"\bв разговоре с кандидатом я узнал\b",
        r"\b(?:we (?:will )?not be moving forward|"
        r"application (?:was |has been )?(?:rejected|unsuccessful)|"
        r"position (?:has been )?filled|selected another candidate)\b",
        r"\b(?:we will review|if your (?:profile|resume) matches).{0,150}\b(?:contact|reach out)\b",
    )
)

_REVIEW_DRAFT_PATTERN = re.compile(
    r"\b(?:"
    r"зарплат\w*|заработн\w*\s+пла[тн]\w*|оклад\w*|доход\w*|рубл\w*|"
    r"переезд\w*|командиров\w*|"
    r"собеседован\w*|интервью\w*|встреч\w*|созвон\w*|"
    r"пообщ\w*|поговор\w*|разговор\w*|слот\w*|календар\w*|брон\w*|"
    r"дат\w*|врем\w*|час\w*|график\w*|удобн\w*|"
    r"сегодня|завтра|послезавтра|"
    r"понедельник\w*|вторник\w*|сред\w*|четверг\w*|"
    r"пятниц\w*|суббот\w*|воскрес\w*|"
    r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\w*|июн\w*|"
    r"июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|"
    r"salary|compensation|relocation|travel|trip|"
    r"interview|meeting|call|schedule|date|time|slot|calendar|book|"
    r"today|tomorrow|tonight|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|weekday|weekend|"
    r"morning|afternoon|evening|noon|available|free|"
    r"contract|offer"
    r")\b",
    re.I,
)

_SIMPLE_SALARY_EXPECTATION_PATTERN = re.compile(
    r"\b(?:"
    r"ка(?:кая|кие|кой)\s+(?:у\s+вас\s+)?(?:зарплат\w*|заработн\w*\s+пла[тн]\w*|"
    r"оклад\w*|доход\w*)|"
    r"какой\s+уровень\s+(?:зарплат\w*|заработн\w*\s+пла[тн]\w*|доход\w*)|"
    r"зарплатн\w*\s+ожидани\w*|ожидани\w*\s+по\s+зарплат\w*|"
    r"желаем\w*\s+(?:зарплат\w*|заработн\w*\s+пла[тн]\w*|оклад\w*|доход\w*)|"
    r"сколько\s+(?:хотите|ожидаете|рассматриваете)|"
    r"какая\s+зарплата\s+вас\s+устроит|"
    r"salary\s+expectations?|expected\s+salary|desired\s+salary|"
    r"what\s+(?:salary|compensation)\s+do\s+you\s+expect"
    r")\b",
    re.I,
)

_SALARY_NEGOTIATION_PATTERN = re.compile(
    r"\b(?:"
    r"предлага\w*|готовы\s+(?:ли\s+)?(?:согласиться|рассмотреть)|согласн\w*|"
    r"устроит\s+ли|вилк\w*|торг\w*|обсуд\w*\s+услови\w*|"
    r"снизить|повысить|выше|ниже|пересмотр\w*|"
    r"offer|agree|accept|negotiate|range|higher|lower"
    r")\b",
    re.I,
)

_EXACT_120_NET_RESPONSE_PATTERN = re.compile(
    r"^(?:здравствуйте[!.\s]*)?(?:мои\s+)?(?:зарплатн\w*\s+ожидани\w*\s*[—:-]?\s*)?"
    r"120\s*000\s*(?:руб(?:лей|ля|\.)?|₽)\s+на\s+руки[.!]?$",
    re.I,
)

_MANUAL_ACTION_PATTERN = re.compile(
    r"\b(?:"
    r"тест\w*|задани\w*|анкет\w*|опрос\w*|"
    r"документ\w*|паспорт\w*|банк\w*|карт\w*|код\w*|"
    r"оплат\w*|"
    r"zoom|skype|teams|google\s+meet|telegram|whatsapp|"
    r"test|assignment|questionnaire|survey|document|passport|bank|card|code|"
    r"payment"
    r")\b",
    re.I,
)

_MANUAL_ACTION_STRUCTURE = re.compile(
    r"https?://|www\.|"
    r"\b[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}\b|"
    r"(?:\+?\d[\d\s()/-]{7,}\d)",
    re.I,
)

_FORM_ACTION_PATTERN = re.compile(
    r"\b(?:анкет\w*|опрос\w*|questionnaire|survey|forms?)\b",
    re.I,
)

_TEST_ACTION_PATTERN = re.compile(
    r"\b(?:тест\w*|задани\w*|assignment)\b",
    re.I,
)

_EXTERNAL_ACTION_REQUEST_PATTERN = re.compile(
    r"\b(?:"
    r"заполн\w*|прой(?:д|т)\w*|выполн\w*|пришл\w*|отправ\w*|направ\w*|"
    r"загруз\w*|прикреп\w*|подпиш\w*|зарегистр\w*|перейд\w*|"
    r"открой\w*|выбер\w*|заброниру\w*|подключ\w*|напиш\w*\s+в|"
    r"свяж\w*\s+по|"
    r"fill|complete|pass|submit|send|upload|attach|sign|register|"
    r"follow|open|choose|book|join|contact"
    r")\b",
    re.I,
)

_REVIEW_DRAFT_STRUCTURE = re.compile(
    r"(?:[₽$€£]\s*\d|\d[\d\s.,]*\s*[₽$€£])|"
    r"\b\d[\d\s.,]*\s*(?:руб\w*|rub|usd|eur|доллар\w*|евро)\b",
    re.I,
)

_REPLY_REQUEST_PATTERN = re.compile(
    r"[?？]|"
    r"\b(?:подскажите|расскажите|уточните|ответьте|ответить|напишите|пришлите|направьте|"
    r"опишите|укажите|подтвердите|выберите|предоставьте|сообщите)\b|"
    r"\b(?:готовы|можете|хотели|интересно|рассматриваете|доступны|есть)\s+ли\b|"
    r"\b(?:какой|какая|какие|каково|сколько|когда|где|почему|как)\b|"
    r"\b(?:could you|can you|would you|please (?:tell|send|share|confirm|describe)|"
    r"what|when|where|why|how)\b",
    re.I,
)


def classify_recruiter_reply(
    application_state: ApplicationState,
    incoming_text: str,
    proposed_response: str = "",
) -> RecruiterReplyDisposition:
    if application_state in _TERMINAL_APPLICATION_STATES:
        return RecruiterReplyDisposition.NO_REPLY
    normalized = " ".join(incoming_text.split())
    if not normalized or _is_no_reply_message(normalized):
        return RecruiterReplyDisposition.NO_REPLY
    if is_simple_salary_expectation_question(normalized) and (
        not proposed_response or is_exact_120_net_salary_response(proposed_response)
    ):
        return RecruiterReplyDisposition.AUTOMATIC_DRAFT
    if proposed_response and (
        _MANUAL_ACTION_PATTERN.search(proposed_response) is not None
        or _MANUAL_ACTION_STRUCTURE.search(proposed_response) is not None
    ):
        return RecruiterReplyDisposition.MANUAL
    if requires_manual_action(normalized, proposed_response):
        return RecruiterReplyDisposition.MANUAL
    if requires_review_draft(normalized, proposed_response):
        return RecruiterReplyDisposition.REVIEW_DRAFT
    if _REPLY_REQUEST_PATTERN.search(normalized) is not None:
        return RecruiterReplyDisposition.AUTOMATIC_DRAFT
    return RecruiterReplyDisposition.AMBIGUOUS


def _is_no_reply_message(text: str) -> bool:
    closing_spans = [
        match.span()
        for pattern in (_HH_INVITATION_REMINDER_PATTERN, *_NO_REPLY_PATTERNS)
        if (match := pattern.search(text)) is not None
    ]
    if not closing_spans:
        return False
    return all(
        any(start <= request.start() and request.end() <= end for start, end in closing_spans)
        for request in _REPLY_REQUEST_PATTERN.finditer(text)
    )


def repeated_incoming_already_answered(
    messages: Sequence[RecruiterReplyHistoryMessage],
) -> bool:
    incoming_positions = [
        position
        for position, message in enumerate(messages)
        if message.direction is MessageDirection.INCOMING
    ]
    if not incoming_positions:
        return False
    latest_position = incoming_positions[-1]
    normalized = normalize_message(messages[latest_position].body)
    if not normalized:
        return False
    previous_positions = [
        position
        for position in incoming_positions[:-1]
        if normalize_message(messages[position].body) == normalized
    ]
    if len(previous_positions) < 2:
        return False
    return all(
        _direct_reply_state(messages, position) is RecruiterMessageState.SENT
        for position in previous_positions[-2:]
    )


def unresolved_external_action_before_invitation_reminder(
    application_state: ApplicationState,
    messages: Sequence[RecruiterReplyHistoryMessage],
) -> bool:
    return (
        unresolved_external_action_position_before_invitation_reminder(
            application_state,
            messages,
        )
        is not None
    )


def unresolved_action_position_before_invitation_reminder(
    application_state: ApplicationState,
    messages: Sequence[RecruiterReplyHistoryMessage],
) -> int | None:
    incoming_positions = [
        position
        for position, message in enumerate(messages)
        if message.direction is MessageDirection.INCOMING
    ]
    if not incoming_positions:
        return None
    latest_position = incoming_positions[-1]
    if _HH_INVITATION_REMINDER_PATTERN.search(messages[latest_position].body) is None:
        return None
    if any(
        message.direction is MessageDirection.OUTGOING
        and message.state is RecruiterMessageState.SENT
        for message in messages[latest_position + 1 :]
    ):
        return None

    for position in range(latest_position - 1, -1, -1):
        message = messages[position]
        if message.direction is MessageDirection.OUTGOING:
            if message.state is RecruiterMessageState.SENT:
                return None
            continue
        if _HH_INVITATION_REMINDER_PATTERN.search(message.body) is not None:
            continue
        disposition = classify_recruiter_reply(application_state, message.body)
        return position if disposition is not RecruiterReplyDisposition.NO_REPLY else None
    return None


def unresolved_external_action_position_before_invitation_reminder(
    application_state: ApplicationState,
    messages: Sequence[RecruiterReplyHistoryMessage],
) -> int | None:
    position = unresolved_action_position_before_invitation_reminder(
        application_state,
        messages,
    )
    if position is None:
        return None
    return (
        position
        if (
            classify_recruiter_reply(application_state, messages[position].body)
            is RecruiterReplyDisposition.MANUAL
        )
        else None
    )


def _direct_reply_state(
    messages: Sequence[RecruiterReplyHistoryMessage],
    incoming_position: int,
) -> RecruiterMessageState | None:
    for message in messages[incoming_position + 1 :]:
        if message.direction is MessageDirection.INCOMING:
            return None
        if message.direction is MessageDirection.OUTGOING:
            return message.state
    return None


def requires_manual_action(*texts: str) -> bool:
    return any(
        _EXTERNAL_ACTION_REQUEST_PATTERN.search(text) is not None
        and (
            _MANUAL_ACTION_PATTERN.search(text) is not None
            or _MANUAL_ACTION_STRUCTURE.search(text) is not None
        )
        for text in texts
        if text
    )


def requested_external_action_kind(*texts: str) -> RecruiterActionKind | None:
    selected = "\n".join(text for text in texts if text)
    if not selected or not requires_manual_action(selected):
        return None
    if _TEST_ACTION_PATTERN.search(selected) is not None:
        return RecruiterActionKind.TEST_ASSIGNMENT
    if _FORM_ACTION_PATTERN.search(selected) is not None:
        return RecruiterActionKind.EXTERNAL_FORM
    return RecruiterActionKind.EXTERNAL_ACTION


def requires_review_draft(*texts: str) -> bool:
    return any(
        _REVIEW_DRAFT_PATTERN.search(text) is not None
        or _REVIEW_DRAFT_STRUCTURE.search(text) is not None
        for text in texts
        if text
    )


def is_simple_salary_expectation_question(text: str) -> bool:
    normalized = " ".join(text.split())
    return (
        _SIMPLE_SALARY_EXPECTATION_PATTERN.search(normalized) is not None
        and _SALARY_NEGOTIATION_PATTERN.search(normalized) is None
    )


def is_exact_120_net_salary_response(text: str) -> bool:
    return _EXACT_120_NET_RESPONSE_PATTERN.fullmatch(" ".join(text.split())) is not None
