# ruff: noqa: RUF001

from __future__ import annotations

import re
from enum import StrEnum

from hugin.domain.applications import ApplicationState


class RecruiterReplyDisposition(StrEnum):
    NO_REPLY = "NO_REPLY"
    AUTOMATIC_DRAFT = "AUTOMATIC_DRAFT"
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
        r"\bв разговоре с кандидатом я узнал\b",
        r"\b(?:ответьте|ответить)\b.{0,100}\bна приглашени",
        r"\bнапомина\w*\b.{0,120}\bприглашени",
        r"\b(?:we (?:will )?not be moving forward|"
        r"application (?:was |has been )?(?:rejected|unsuccessful)|"
        r"position (?:has been )?filled|selected another candidate)\b",
        r"\b(?:we will review|if your (?:profile|resume) matches).{0,150}\b(?:contact|reach out)\b",
    )
)

_MANUAL_REPLY_PATTERN = re.compile(
    r"\b(?:"
    r"зарплат\w*|оклад\w*|доход\w*|рубл\w*|"
    r"переезд\w*|командиров\w*|"
    r"собеседован\w*|интервью\w*|встреч\w*|созвон\w*|"
    r"пообщ\w*|поговор\w*|разговор\w*|слот\w*|календар\w*|брон\w*|"
    r"дат\w*|врем\w*|час\w*|график\w*|удобн\w*|"
    r"сегодня|завтра|послезавтра|"
    r"понедельник\w*|вторник\w*|сред\w*|четверг\w*|"
    r"пятниц\w*|суббот\w*|воскрес\w*|"
    r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\w*|июн\w*|"
    r"июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|"
    r"тестов\w*|задани\w*|"
    r"документ\w*|паспорт\w*|банк\w*|карт\w*|код\w*|"
    r"оплат\w*|ссылк\w*|файл\w*|договор\w*|оффер\w*|"
    r"zoom|skype|teams|google\s+meet|telegram|whatsapp|"
    r"salary|compensation|relocation|travel|trip|"
    r"interview|meeting|call|schedule|date|time|slot|calendar|book|"
    r"today|tomorrow|tonight|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|weekday|weekend|"
    r"morning|afternoon|evening|noon|available|free|join|"
    r"test|assignment|document|passport|bank|card|code|"
    r"payment|link|file|contract|offer"
    r")\b",
    re.I,
)

_MANUAL_REPLY_STRUCTURE = re.compile(
    r"https?://|www\.|"
    r"\b[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}\b|"
    r"\b\d{1,2}:\d{2}\b|"
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
    r"(?:\+?\d[\d\s()/-]{7,}\d)|"
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
    if requires_manual_reply(normalized, proposed_response):
        return RecruiterReplyDisposition.MANUAL
    if _REPLY_REQUEST_PATTERN.search(normalized) is not None:
        return RecruiterReplyDisposition.AUTOMATIC_DRAFT
    return RecruiterReplyDisposition.NO_REPLY


def requires_manual_reply(*texts: str) -> bool:
    return any(
        _MANUAL_REPLY_PATTERN.search(text) is not None
        or _MANUAL_REPLY_STRUCTURE.search(text) is not None
        for text in texts
        if text
    )
