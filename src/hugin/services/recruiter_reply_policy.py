# ruff: noqa: RUF001

from __future__ import annotations

import re
from enum import StrEnum

from hugin.domain.applications import ApplicationState


class RecruiterReplyDisposition(StrEnum):
    NO_REPLY = "NO_REPLY"
    AUTOMATIC_DRAFT = "AUTOMATIC_DRAFT"
    REVIEW_DRAFT = "REVIEW_DRAFT"
    MANUAL = "MANUAL"


_TERMINAL_APPLICATION_STATES = {
    ApplicationState.REJECTED,
    ApplicationState.CLOSED,
}

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
        r"\bесли\b.{0,150}\b(?:подойд|заинтерес)\w*\b.{0,150}\b(?:свяж|напиш|позвон)",
        r"\bпредставител\w* работодател\w*\b.{0,150}\b(?:ознаком|свяж)",
        r"\b(?:опрос|анкет\w*)\b.{0,100}\b(?:заверш|пройден|получен)",
        r"\bблагодар\w*\b.{0,120}\b(?:за ответы|за прохождение|ответы получены)",
        r"\b(?:ии|ai)[ -]?(?:помощник|ассистент)\b.{0,120}\bзавершил\w*\s+работ",
        r"\bберу\s+время\b.{0,180}\b(?:изучен|разбор|рассмотрен)\w*\b",
        r"\bв разговоре с кандидатом я узнал\b",
        r"\b(?:ответьте|ответить)\b.{0,100}\bна приглашени",
        r"\bнапомина\w*\b.{0,120}\bприглашени",
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
    r"тестов\w*|задани\w*|анкет\w*|опрос\w*|"
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

_EXTERNAL_ACTION_REQUEST_PATTERN = re.compile(
    r"\b(?:"
    r"заполн\w*|пройд\w*|выполн\w*|пришл\w*|отправ\w*|направ\w*|"
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
    r"\b(?:подскажите|расскажите|уточните|ответьте|напишите|пришлите|направьте|"
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
    if not normalized or any(pattern.search(normalized) for pattern in _NO_REPLY_PATTERNS):
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
    return RecruiterReplyDisposition.NO_REPLY


def requires_manual_reply(*texts: str) -> bool:
    return requires_manual_action(*texts) or requires_review_draft(*texts)


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
