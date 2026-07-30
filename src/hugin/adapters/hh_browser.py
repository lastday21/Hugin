from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from math import ceil
from pathlib import Path
from types import TracebackType
from urllib.parse import urlencode, urlparse, urlunparse

from playwright.sync_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    Response,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from hugin.domain.communications import MessageSendOutcome, MessageSendResult
from hugin.domain.content import MessageDirection
from hugin.domain.hh import (
    HhApplyResult,
    HhApplyStatus,
    HhFormReviewResult,
    HhFormReviewStatus,
    HhProfileData,
    HhResumeData,
    HhResumeDetails,
    HhResumeExperienceBlock,
    HhScreeningField,
    HhScreeningForm,
    screening_form_hash,
)
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhNegotiationStatus,
    HhSyncBlockedError,
)
from hugin.domain.vacancies import VacancyAvailability, VacancyData, VacancySearchResult
from hugin.services.hh_login import HhCredentials, LoginStatus

PROFILE_SNAPSHOT_SCRIPT = """
() => {
    const states = Array.from(
        document.querySelectorAll('template.ResumeProfileFront-InitialState')
    ).flatMap((template) => {
        try {
            return [JSON.parse(template.content.textContent || '')];
        } catch {
            return [];
        }
    });
    const account = states.find((state) => state.userId != null) || {};
    const profile = states.find((state) => state.profile != null)?.profile || {};
    const fields = profile.fields || {};
    const fieldValue = (name) => {
        const value = fields[name]?.[0]?.string;
        return typeof value === 'string' ? value.trim() : '';
    };
    const resumes = Array.from(document.querySelectorAll('[data-qa="resume"]')).map(
        (card) => ({
            title: (
                card.querySelector('[data-qa="resume-title"]')?.textContent || ''
            ).trim(),
            href: card.querySelector(
                'a[data-qa^="resume-card-link-"][href*="/resume/"]'
            )?.href || '',
        })
    );
    return {
        externalId: account.userId == null ? '' : String(account.userId),
        firstName: fieldValue('firstName'),
        lastName: fieldValue('lastName'),
        resumes,
    };
}
"""

VACANCY_SEARCH_SCRIPT = """
() => ({
    header: (
        document.querySelector('[data-qa="vacancies-search-header"]')?.textContent || ''
    ).trim(),
    vacancies: Array.from(
        document.querySelectorAll('[data-qa="vacancy-serp__vacancy"]')
    ).map((card) => ({
        title: (
            card.querySelector('[data-qa="serp-item__title"]')?.textContent || ''
        ).trim(),
        href: card.querySelector('[data-qa="serp-item__title"]')?.href || '',
        employer: (
            card.querySelector(
                '[data-qa="vacancy-serp__vacancy-employer"]'
            )?.textContent || ''
        ).trim(),
        region: (
            card.querySelector('[data-qa="vacancy-serp__vacancy-address"]')?.textContent || ''
        ).trim(),
        salary: (
            card.querySelector('[data-qa="vacancy-serp__vacancy-compensation"]')?.textContent || ''
        ).trim(),
        publishedAt: card.querySelector('time[datetime]')?.getAttribute('datetime') || '',
    })),
})
"""

VACANCY_DETAILS_SCRIPT = """
() => {
const description = document.querySelector('[data-qa="vacancy-description"]');
const bodyText = (document.body.innerText || '').trim();
const normalizedBody = bodyText.toLocaleLowerCase('ru-RU');
const externalLinks = Array.from(description?.querySelectorAll('a[href]') || []).filter((link) => {
    try {
        const host = new URL(link.href, window.location.href).hostname;
        return host !== 'hh.ru' && !host.endsWith('.hh.ru');
    } catch {
        return false;
    }
});
let availability = 'ACTIVE';
if (normalizedBody.includes('вакансия в архиве')) availability = 'ARCHIVED';
else if (normalizedBody.includes('вакансия закрыта')) availability = 'CLOSED';
else if (normalizedBody.includes('вакансия недоступна')) availability = 'UNAVAILABLE';
return ({
    title: (
        document.querySelector('[data-qa="vacancy-title"]')?.textContent || ''
    ).trim(),
    employer: (
        document.querySelector('[data-qa="vacancy-company-name"]')?.textContent || ''
    ).trim(),
    experience: (
        document.querySelector('[data-qa="vacancy-experience"]')?.textContent || ''
    ).trim(),
    employment: (
        document.querySelector('[data-qa="common-employment-text"]')?.textContent || ''
    ).trim(),
    workFormat: (
        document.querySelector('[data-qa="work-formats-text"]')?.textContent || ''
    ).trim(),
    description: (
        description?.innerText || ''
    ).trim(),
    skills: Array.from(document.querySelectorAll('[data-qa="skills-element"]'))
        .map((element) => (element.textContent || '').trim())
        .filter(Boolean),
    region: (
        document.querySelector('[data-qa="vacancy-view-location"]')?.textContent || ''
    ).trim(),
    address: (
        document.querySelector('[data-qa="vacancy-view-raw-address"]')?.textContent || ''
    ).trim(),
    salary: (
        document.querySelector('[data-qa="vacancy-salary"]')?.textContent || ''
    ).trim(),
    schedule: (
        document.querySelector('[data-qa="vacancy-view-employment-mode"]')?.textContent || ''
    ).trim(),
    publishedAt: (
        document.querySelector('[data-qa="vacancy-creation-time"] time[datetime]')
            ?.getAttribute('datetime') ||
        document.querySelector('time[datetime]')?.getAttribute('datetime') || ''
    ),
    hasCoverLetter: normalizedBody.includes('сопроводительн') && normalizedBody.includes('письм'),
    hasScreeningForm: normalizedBody.includes('вопросы работодателя') ||
        Boolean(document.querySelector('[data-qa="task-question"]')),
    hasExternalLink: externalLinks.length > 0,
    hasTestAssignment: normalizedBody.includes('тестовое задание') ||
        normalizedBody.includes('испытательное задание'),
    availability,
})}
"""

RESUME_DETAILS_SCRIPT = """
async () => {
const actionLine = /^(?:развернуть|свернуть|добавить|редактировать|указать уровни?)$/i;
const clean = (value) => (value || '')
    .replace(/\\u00a0/g, ' ')
    .split('\\n')
    .map((line) => line.trim().replace(/[ \\t]+/g, ' '))
    .filter((line) => !actionLine.test(line))
    .join('\\n')
    .trim()
    .replace(/\\n{3,}/g, '\\n\\n');
const states = Array.from(
    document.querySelectorAll('template.ResumeProfileFront-InitialState')
).flatMap((template) => {
    try {
        return [JSON.parse(template.content.textContent || '{}')];
    } catch {
        return [];
    }
});
const resumeState = states.find((state) => state?.scheme?.resume)?.scheme?.resume || {};
const stateValues = (key) => {
    const values = resumeState[key];
    if (!Array.isArray(values)) return [];
    return values
        .map((item) => item?.string)
        .filter((value) => typeof value === 'string' || typeof value === 'number');
};
const stateText = (key) => clean(stateValues(key)[0] || '');
const labels = (values, known) => Array.from(new Set(
    values.map((value) => known[String(value)] || clean(String(value))).filter(Boolean)
)).join(', ');
const firstText = (...selectors) => {
    for (const selector of selectors) {
        const node = document.querySelector(selector);
        const value = clean(node?.innerText || node?.textContent || '');
        if (value) return value;
    }
    return '';
};
const allText = (...selectors) => {
    const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
    const leaves = nodes.filter((node, index, all) =>
        !all.some((other, otherIndex) =>
            index !== otherIndex && node !== other && node.contains(other)
        )
    );
    return Array.from(new Set(
        leaves.map((node) => clean(node.innerText || node.textContent || '')).filter(Boolean)
    )).join(', ');
};
const mainText = clean(document.querySelector('main')?.innerText || document.body?.innerText || '');
const fragment = (pattern) => {
    for (const part of mainText.split(/\\n|,/).map(clean)) {
        if (pattern.test(part)) return part;
    }
    return '';
};
const blockAfterHeading = (headingPattern) => {
    const headings = Array.from(document.querySelectorAll(
        'h1, h2, h3, h4, [role="heading"], [data-qa*="title"]'
    ));
    const heading = headings.find((node) => headingPattern.test(clean(node.textContent || '')));
    if (!heading) return '';
    const container = heading.closest('section, article, [data-qa*="about"], [data-qa*="skills"]');
    if (!container) return '';
    const value = clean(container.innerText || '');
    const title = clean(heading.textContent || '');
    return clean(value.startsWith(title) ? value.slice(title.length) : value);
};
const areaId = stateValues('area')[0];
const stateAreaName = (() => {
    if (areaId === undefined) return '';
    const queue = states.map((value) => ({value, path: ''}));
    for (let index = 0; index < queue.length; index += 1) {
        const current = queue[index];
        if (!current.value || typeof current.value !== 'object') continue;
        const pathIsArea = /(?:area|region|city)/i.test(current.path);
        if (pathIsArea && !Array.isArray(current.value)) {
            const identifier = current.value.id ?? current.value.value ?? current.value.code;
            if (String(identifier) === String(areaId)) {
                for (const key of ['name', 'text', 'label', 'title']) {
                    const candidate = clean(current.value[key]);
                    if (candidate && candidate !== String(areaId)) return candidate;
                }
            }
        }
        for (const [key, value] of Object.entries(current.value)) {
            if (value && typeof value === 'object') {
                queue.push({value, path: current.path ? `${current.path}.${key}` : key});
            } else if (
                pathIsArea &&
                key === String(areaId) &&
                typeof value === 'string' &&
                clean(value)
            ) {
                return clean(value);
            }
        }
    }
    return '';
})();
const apiAreaName = await (async () => {
    if (stateAreaName || areaId === undefined || !/^\\d+$/.test(String(areaId))) return '';
    try {
        const response = await fetch(`https://api.hh.ru/areas/${areaId}`);
        if (!response.ok) return '';
        const area = await response.json();
        return clean(area?.name);
    } catch {
        return '';
    }
})();
const monthNames = [
    'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
    'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'
];
const monthYear = (value) => {
    const match = /^(\\d{4})-(\\d{2})/.exec(value || '');
    if (!match) return clean(value);
    const month = monthNames[Number(match[2]) - 1] || match[2];
    return `${month} ${match[1]}`;
};
const stateExperienceBlocks = (
    Array.isArray(resumeState.experience) ? resumeState.experience : []
).map((item) => {
    const company = clean(item?.companyName);
    const position = clean(item?.position);
    const start = monthYear(item?.startDate);
    const end = item?.endDate ? monthYear(item.endDate) : 'настоящее время';
    const period = start ? `${start} — ${end}` : '';
    const description = clean(item?.description);
    return {
        company,
        position,
        period,
        description,
        text: clean([company, position, period, description].filter(Boolean).join('\\n'))
    };
}).filter((block) => block.text);
const candidateNodes = Array.from(document.querySelectorAll([
    '[data-qa="profile-experience-company-card"]',
    '[data-qa="resume-list-card-experience"]',
    '[data-qa="resume-block-experience"]',
    '[data-qa^="resume-block-experience-"]',
    '[data-qa^="resume-experience-item-"]'
].join(',')));
const experienceNodes = candidateNodes.filter((node, index, all) =>
    !all.some((other, otherIndex) =>
        otherIndex !== index && node.contains(other) && other !== node
    )
);
const childText = (node, selectors) => {
    for (const selector of selectors) {
        const child = node.querySelector(selector);
        const value = clean(child?.innerText || child?.textContent || '');
        if (value) return value;
    }
    return '';
};
const experienceBlocks = experienceNodes.map((node) => {
    const text = clean(node.innerText || node.textContent || '');
    return {
        company: childText(node, [
            '[data-qa*="experience-company"]',
            '[data-qa*="experience-organisation"]',
            '[data-qa*="experience-employer"]'
        ]),
        position: childText(node, [
            '[data-qa*="experience-position"]',
            '[data-qa*="experience-title"]'
        ]),
        period: childText(node, [
            '[data-qa*="experience-period"]',
            '[data-qa*="experience-date"]',
            '[data-qa*="time-interval"]'
        ]),
        description: childText(node, [
            '[data-qa*="experience-description"]',
            '[data-qa*="experience-responsibility"]'
        ]),
        text
    };
}).filter((block) => block.text);
const structuredExperienceBlocks = stateExperienceBlocks.length
    ? stateExperienceBlocks
    : experienceBlocks;
const experience = structuredExperienceBlocks.map((block) => block.text).join('\\n\\n') ||
    firstText('[data-qa="resume-list-card-experience"]');
const employment = labels(stateValues('employment'), {
    full: 'Полная занятость',
    part: 'Частичная занятость',
    project: 'Проектная работа',
    volunteer: 'Волонтёрство',
    probation: 'Стажировка'
}) || labels(stateValues('employmentForms'), {
    FULL: 'Постоянная работа',
    PART_TIME: 'Подработка',
    PROJECT: 'Проектная работа',
    INTERNSHIP: 'Стажировка'
}) || allText(
    '[data-qa="resume-position-field-employmentForms"]',
    '[data-qa="resume-block-employment"]',
    '[data-qa*="resume-employment"]'
);
const workFormat = labels(stateValues('workFormats'), {
    ON_SITE: 'На месте работодателя',
    REMOTE: 'Удалённо',
    HYBRID: 'Гибрид'
}) || allText(
    '[data-qa="resume-position-field-workFormats"]',
    '[data-qa="resume-block-work-schedule"]',
    '[data-qa="resume-block-work-format"]',
    '[data-qa*="resume-work-format"]'
);
const relocation = labels(stateValues('relocation'), {
    no_relocation: 'Не готов к переезду',
    relocation_possible: 'Готов к переезду',
    relocation_desirable: 'Хочу переехать'
}) || fragment(/переезд/i);
const businessTrips = labels(stateValues('businessTripReadiness'), {
    never: 'Не готов к командировкам',
    sometimes: 'Готов к редким командировкам',
    ready: 'Готов к командировкам'
}) || fragment(/командиров/i);
const stateSkills = stateValues('keySkills').map((value) => clean(String(value))).filter(Boolean);
return {
    title: firstText(
        '[data-qa="resume-block-title-position"]',
        '[data-qa*="resume-title"]'
    ) || stateText('title'),
    city: firstText(
        '[data-qa="resume-personal-address"]',
        '[data-qa="resume-personal-location"]',
        '[data-qa="resume-block-location"]',
        '[data-qa*="resume-address"]',
        '[data-qa*="resume-location"]'
    ) || stateAreaName || apiAreaName || fragment(/^(?:Проживает|Город)\\s*:/i).replace(
        /^(?:Проживает|Город)\\s*:\\s*/i, ''
    ),
    salary: firstText(
        '[data-qa="resume-block-salary"]',
        '[data-qa="resume-block-title-salary"]',
        '[data-qa*="resume-salary"]'
    ) || fragment(/(?:зарплат|доход)/i),
    employment,
    workFormat,
    relocation,
    businessTrips,
    experience,
    experienceBlocks: structuredExperienceBlocks,
    skills: stateSkills.join(', ') ||
        allText('[data-qa^="skill-tag-"]') ||
        firstText('[data-qa="skills-card"]', '[data-qa*="resume-skills"]') ||
        blockAfterHeading(/^Навыки$/i),
    education: firstText(
        '[data-qa="resume-list-card-education"]',
        '[data-qa*="resume-education"]'
    ) || blockAfterHeading(/^Образование$/i),
    about: stateText('skills') || firstText(
        '[data-qa="resume-block-about"]',
        '[data-qa="resume-about-card"]',
        '[data-qa*="resume-about"]'
    ) || blockAfterHeading(/^Обо мне$/i)
};
}
"""

APPLICATION_FORM_SCRIPT = """
() => {
const clean = (value) => (value || '').trim().replace(/\\s+/g, ' ');
const questionNodes = Array.from(document.querySelectorAll('[data-qa="task-question"]'));
const fieldFromNode = (node, position) => {
    const controls = Array.from(node.querySelectorAll(
        'textarea, select, input:not([type="hidden"]), [role="combobox"]'
    ));
    const control = controls[0] || null;
    const question = clean(
        node.querySelector('label, legend, [data-qa*="question-title"]')?.textContent ||
        node.innerText
    );
    const qa = clean(control?.getAttribute('data-qa'));
    const name = clean(control?.getAttribute('name'));
    const id = clean(control?.getAttribute('id'));
    const key = (
        qa ? `${position}:qa:${qa}` : name ? `${position}:name:${name}` :
        id ? `${position}:id:${id}` :
        `question:${position}:${question.toLocaleLowerCase('ru-RU')}`
    ).slice(0, 255);
    const tag = (control?.tagName || '').toLocaleLowerCase('en-US');
    const inputType = clean(control?.getAttribute('type')).toLocaleLowerCase('en-US');
    let fieldType = tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : inputType;
    if (!fieldType && control?.getAttribute('role') === 'combobox') fieldType = 'combobox';
    if (!fieldType) fieldType = control ? 'text' : 'unknown';
    const optionControls = Array.from(node.querySelectorAll('input[type="radio"]'));
    const options = tag === 'select'
        ? Array.from(control.options || []).map(
            (option) => clean(option.textContent || option.value)
        )
            .filter(Boolean)
        : optionControls.map((option) => clean(
            option.closest('label')?.innerText || option.value
        )).filter(Boolean);
    if (optionControls.length) fieldType = 'radio';
    const maxLengthValue = Number.parseInt(control?.getAttribute('maxlength') || '', 10);
    const normalized = question.toLocaleLowerCase('ru-RU');
    return {
        key,
        question,
        fieldType,
        isRequired: Boolean(
            control?.required || control?.getAttribute('aria-required') === 'true' ||
            /(^|\\s)\\*(\\s|$)/.test(question)
        ),
        options,
        maxLength: Number.isFinite(maxLengthValue) && maxLengthValue > 0 ? maxLengthValue : null,
        formatHint: clean(
            control?.getAttribute('placeholder') || control?.getAttribute('inputmode')
        ),
        hasAttachment: Boolean(node.querySelector('input[type="file"]')),
        hasExternalAction: Boolean(node.querySelector('a[href]')),
        hasTestAssignment: normalized.includes('тестов') || normalized.includes('испытательн'),
    };
};
return ({
    fields: questionNodes.map(fieldFromNode).filter((field) => field.question),
    warnings: Array.from(document.querySelectorAll('[data-qa="response-reject-warning"]')).map(
        (node) => (node.innerText || '').trim().replace(/\\s+/g, ' ')
    ).filter(Boolean),
    resumeTitle: (
        document.querySelector('[data-qa="resume-title"]')?.textContent || ''
    ).trim(),
    bodyText: (document.body.innerText || '').trim(),
});
}
"""

FILL_APPLICATION_FORM_SCRIPT = """
(answers) => {
const clean = (value) => (value || '').trim().replace(/\\s+/g, ' ');
const normalized = (value) => clean(value).toLocaleLowerCase('ru-RU');
const nodes = Array.from(document.querySelectorAll('[data-qa="task-question"]'));
const controls = nodes.map((node, position) => {
    const items = Array.from(node.querySelectorAll(
        'textarea, select, input:not([type="hidden"]), [role="combobox"]'
    ));
    const control = items[0] || null;
    const question = clean(
        node.querySelector('label, legend, [data-qa*="question-title"]')?.textContent ||
        node.innerText
    );
    const qa = clean(control?.getAttribute('data-qa'));
    const name = clean(control?.getAttribute('name'));
    const id = clean(control?.getAttribute('id'));
    const key = (
        qa ? `${position}:qa:${qa}` : name ? `${position}:name:${name}` :
        id ? `${position}:id:${id}` :
        `question:${position}:${question.toLocaleLowerCase('ru-RU')}`
    ).slice(0, 255);
    return {key, node, control};
});
const setValue = (control, value) => {
    const prototype = control instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
    if (setter) setter.call(control, value); else control.value = value;
    control.dispatchEvent(new Event('input', {bubbles: true}));
    control.dispatchEvent(new Event('change', {bubbles: true}));
};
const filled = [];
const skipped = [];
for (const answer of answers) {
    const item = controls.find((candidate) => candidate.key === answer.key);
    const value = clean(answer.value);
    if (!item?.control || !value || item.node.querySelector('input[type="file"]')) {
        skipped.push(answer.key);
        continue;
    }
    const control = item.control;
    const tag = control.tagName.toLocaleLowerCase('en-US');
    const type = clean(control.getAttribute('type')).toLocaleLowerCase('en-US');
    if (tag === 'select') {
        const option = Array.from(control.options).find(
            (candidate) => normalized(candidate.value) === normalized(value) ||
                normalized(candidate.textContent) === normalized(value)
        );
        if (!option) { skipped.push(answer.key); continue; }
        control.value = option.value;
        control.dispatchEvent(new Event('change', {bubbles: true}));
    } else if (type === 'radio') {
        const radio = Array.from(item.node.querySelectorAll('input[type="radio"]')).find(
            (candidate) => normalized(candidate.value) === normalized(value) ||
                normalized(candidate.closest('label')?.innerText) === normalized(value)
        );
        if (!radio) { skipped.push(answer.key); continue; }
        radio.click();
    } else if (type === 'checkbox') {
        const shouldCheck = ['да', 'true', '1', 'согласен'].includes(normalized(value));
        if (control.checked !== shouldCheck) control.click();
    } else if (control.getAttribute('role') === 'combobox') {
        skipped.push(answer.key);
        continue;
    } else {
        setValue(control, value);
    }
    filled.push(answer.key);
}
return {filled, skipped};
}
"""

NEGOTIATIONS_SCRIPT = """
() => Array.from(document.querySelectorAll('[data-qa="negotiations-item"]')).map((item) => {
    const vacancy = item.querySelector('[data-qa="negotiations-item-vacancy"]');
    const anchor = vacancy?.closest('a') || vacancy?.querySelector('a[href*="/vacancy/"]');
    const tag = item.querySelector('[data-qa^="negotiations-tag"]');
    const fiberKey = Object.getOwnPropertyNames(item).find(
        (key) => key.startsWith('__reactFiber$')
    );
    let fiber = fiberKey ? item[fiberKey] : null;
    let vacancyId = '';
    for (let level = 0; fiber && level < 12 && !vacancyId; level += 1) {
        const value = (
            fiber.memoizedProps?.topic?.vacancyId ||
            fiber.pendingProps?.topic?.vacancyId
        );
        if (typeof value === 'number' || typeof value === 'string') {
            vacancyId = String(value);
        }
        fiber = fiber.return;
    }
    return {
        vacancyId,
        vacancyHref: anchor?.getAttribute('href') || '',
        statusQa: tag?.getAttribute('data-qa') || '',
        statusLabel: (tag?.textContent || '').trim().replace(/\\s+/g, ' '),
        chatAvailable: Boolean(item.querySelector('[data-qa="open_chat"]')),
    };
})
"""

OPEN_NEGOTIATION_CHAT_SCRIPT = """
(vacancyId) => {
    const item = Array.from(
        document.querySelectorAll('[data-qa="negotiations-item"]')
    ).find((candidate) => {
        const link = candidate.querySelector('a[href*="/vacancy/"]');
        return link && new URL(link.href, location.href).pathname === `/vacancy/${vacancyId}`;
    });
    const button = item?.querySelector('[data-qa="open_chat"]');
    if (!button) return false;
    button.click();
    return true;
}
"""

CHAT_MESSAGES_SCRIPT = """
(vacancyId) => Array.from(
    document.querySelectorAll(
        '[data-qa^="chatik-chat-message-"]:not([data-qa$="-text"])'
    )
).map((message) => {
    const qa = message.getAttribute('data-qa') || '';
    const own = Boolean(
        message.querySelector('[data-qa="desktop-message-menu-wrapper"], ' +
            '[data-qa="chat-bubble-icon-read"]')
    );
    return {
        vacancyId,
        messageId: qa.replace('chatik-chat-message-', ''),
        direction: own ? 'OUTGOING' : 'INCOMING',
        body: (
            message.querySelector('[data-qa="chat-bubble-text"]')?.textContent || ''
        ).trim(),
        displayedTime: (
            message.querySelector('[data-qa="chat-buble-display-time"]')?.textContent || ''
        ).trim(),
    };
}).filter((message) => message.messageId && message.body)
"""

ALLOWED_SEARCH_FILTERS = frozenset(
    {
        "currency",
        "employment",
        "employment_form",
        "excluded_text",
        "experience",
        "label",
        "only_with_salary",
        "order_by",
        "professional_role",
        "salary",
        "schedule",
        "search_field",
        "work_format",
    }
)


@dataclass(frozen=True, slots=True)
class _ApplicationSnapshot:
    screening_form: HhScreeningForm
    resume_title: str
    body_text: str

    @property
    def questions(self) -> tuple[str, ...]:
        return tuple(field.question for field in self.screening_form.fields)

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.screening_form.warnings


@dataclass(slots=True)
class _SubmissionAttempt:
    started: bool = False


class _ResumeSelectionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class VisibleHhBrowser:
    def __init__(
        self,
        profile_dir: Path,
        login_url: str,
        resumes_url: str,
        search_url: str,
        timeout_ms: int,
        *,
        start_minimized: bool = False,
    ) -> None:
        self._profile_dir = profile_dir
        self._login_url = login_url
        self._resumes_url = resumes_url
        self._search_url = search_url
        self._timeout_ms = timeout_ms
        self._start_minimized = start_minimized
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> VisibleHhBrowser:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            chromium_args = (
                [
                    "--start-minimized",
                    "--mute-audio",
                ]
                if self._start_minimized
                else ["--start-maximized"]
            )
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir),
                headless=False,
                no_viewport=True,
                args=chromium_args,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self._timeout_ms)
            self._page.set_default_navigation_timeout(self._timeout_ms)
            self._minimize_window()
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        return self

    def _minimize_window(self) -> None:
        if not self._start_minimized or self._context is None or self._page is None:
            return
        with suppress(PlaywrightError, AttributeError, KeyError, TypeError):
            session = self._context.new_cdp_session(self._page)
            try:
                window = session.send("Browser.getWindowForTarget")
                window_id = window["windowId"]
                if not isinstance(window_id, int):
                    raise TypeError
                session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "minimized"},
                    },
                )
            finally:
                session.detach()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def open_login(self) -> None:
        page = self._require_page()
        try:
            page.goto(self._login_url, wait_until="domcontentloaded")
        except PlaywrightError as error:
            if "ERR_ABORTED" not in str(error):
                raise
            page.wait_for_timeout(500)
            if not self.is_authenticated():
                raise

    def read_profile(self) -> HhProfileData:
        page = self._require_page()
        page.goto(self._resumes_url, wait_until="domcontentloaded")
        page.locator("template.ResumeProfileFront-InitialState").first.wait_for(
            state="attached",
            timeout=self._timeout_ms,
        )
        payload = page.evaluate(PROFILE_SNAPSHOT_SCRIPT)
        if not isinstance(payload, dict):
            raise RuntimeError("hh.ru вернул некорректные данные профиля")

        external_id = self._required_string(payload, "externalId", "аккаунта")
        first_name = self._optional_string(payload, "firstName")
        last_name = self._optional_string(payload, "lastName")
        label = " ".join(part for part in (first_name, last_name) if part)
        if not label:
            label = "Аккаунт hh.ru"

        raw_resumes = payload.get("resumes")
        if not isinstance(raw_resumes, list):
            raise RuntimeError("hh.ru вернул некорректный список резюме")

        resumes: list[HhResumeData] = []
        for raw_resume in raw_resumes:
            if not isinstance(raw_resume, dict):
                raise RuntimeError("hh.ru вернул некорректное резюме")
            title = self._required_string(raw_resume, "title", "резюме")
            href = self._required_string(raw_resume, "href", "ссылки на резюме")
            resumes.append(HhResumeData(hh_id=self._resume_id(href), title=title))

        return HhProfileData(
            external_id=external_id,
            label=label,
            resumes=tuple(resumes),
        )

    def search_vacancies(
        self,
        query: str,
        *,
        area: str = "",
        filters: dict[str, object] | None = None,
        page_number: int = 0,
    ) -> VacancySearchResult:
        if not query.strip():
            raise ValueError("Поисковая фраза не может быть пустой")
        if page_number < 0:
            raise ValueError("Номер страницы не может быть отрицательным")

        parameters: list[tuple[str, str]] = [("text", query.strip())]
        if area:
            parameters.append(("area", area))
        parameters.append(("page", str(page_number)))
        parameters.extend(self._search_filters(filters or {}))
        separator = "&" if urlparse(self._search_url).query else "?"
        url = f"{self._search_url}{separator}{urlencode(parameters)}"

        page = self._require_page()
        page.goto(url, wait_until="domcontentloaded")
        page.locator('[data-qa="vacancies-search-header"]').first.wait_for(
            state="visible",
            timeout=self._timeout_ms,
        )
        payload = page.evaluate(VACANCY_SEARCH_SCRIPT)
        if not isinstance(payload, dict):
            raise RuntimeError("hh.ru вернул некорректные результаты поиска")

        header = self._required_string(payload, "header", "результатов поиска")
        raw_vacancies = payload.get("vacancies")
        if not isinstance(raw_vacancies, list):
            raise RuntimeError("hh.ru вернул некорректный список вакансий")

        vacancies: list[VacancyData] = []
        for raw_vacancy in raw_vacancies:
            if not isinstance(raw_vacancy, dict):
                raise RuntimeError("hh.ru вернул некорректную вакансию")
            title = self._required_string(raw_vacancy, "title", "названия вакансии")
            href = self._required_string(raw_vacancy, "href", "ссылки на вакансию")
            employer = self._optional_string(raw_vacancy, "employer") or None
            salary = self._salary(self._optional_string(raw_vacancy, "salary"))
            vacancy_id, source_url = self._vacancy_id_and_url(href)
            vacancies.append(
                VacancyData(
                    hh_id=vacancy_id,
                    title=title,
                    source_url=source_url,
                    employer_name=employer,
                    region=self._optional_string(raw_vacancy, "region") or None,
                    salary_from=salary[0],
                    salary_to=salary[1],
                    salary_currency=salary[2],
                    salary_gross=salary[3],
                    published_at=self._date_time(self._optional_string(raw_vacancy, "publishedAt")),
                )
            )

        return VacancySearchResult(
            found=self._found_vacancies(header, has_items=bool(vacancies)),
            vacancies=tuple(vacancies),
        )

    def read_vacancy_details(self, source_url: str) -> VacancyData:
        vacancy_id, normalized_url = self._vacancy_id_and_url(source_url)
        page = self._require_page()
        try:
            page.goto(normalized_url, wait_until="domcontentloaded")
            page.locator('[data-qa="vacancy-title"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"Страница вакансии {vacancy_id} не загрузилась") from error
        payload = page.evaluate(VACANCY_DETAILS_SCRIPT)
        if not isinstance(payload, dict):
            raise RuntimeError("hh.ru вернул некорректные подробности вакансии")

        raw_skills = payload.get("skills")
        if not isinstance(raw_skills, list) or not all(
            isinstance(skill, str) for skill in raw_skills
        ):
            raise RuntimeError("hh.ru вернул некорректный список навыков")

        description = self._optional_string(payload, "description") or None
        responsibilities, required, preferred = self._description_sections(description or "")
        salary = self._salary(self._optional_string(payload, "salary"))
        raw_availability = self._optional_string(payload, "availability") or "ACTIVE"
        try:
            availability = VacancyAvailability(raw_availability)
        except ValueError as error:
            raise RuntimeError("hh.ru вернул некорректное состояние вакансии") from error

        return VacancyData(
            hh_id=vacancy_id,
            title=self._required_string(payload, "title", "названия вакансии"),
            source_url=normalized_url,
            employer_name=self._optional_string(payload, "employer") or None,
            description=description,
            experience=self._optional_string(payload, "experience") or None,
            employment=self._optional_string(payload, "employment") or None,
            work_format=self._optional_string(payload, "workFormat") or None,
            key_skills=tuple(skill.strip() for skill in raw_skills if skill.strip()),
            details_fetched_at=datetime.now(UTC),
            region=self._optional_string(payload, "region") or None,
            address=self._optional_string(payload, "address") or None,
            salary_from=salary[0],
            salary_to=salary[1],
            salary_currency=salary[2],
            salary_gross=salary[3],
            schedule=self._optional_string(payload, "schedule") or None,
            responsibilities=responsibilities,
            required_qualifications=required,
            preferred_qualifications=preferred,
            has_cover_letter=payload.get("hasCoverLetter") is True,
            has_screening_form=payload.get("hasScreeningForm") is True,
            has_external_link=payload.get("hasExternalLink") is True,
            has_test_assignment=payload.get("hasTestAssignment") is True,
            availability=availability,
            published_at=self._date_time(self._optional_string(payload, "publishedAt")),
        )

    def read_resume_details(self, resume_id: str) -> HhResumeDetails:
        if not resume_id or len(resume_id) > 64 or re.fullmatch(r"[A-Za-z0-9]+", resume_id) is None:
            raise ValueError("Некорректный идентификатор резюме hh.ru")
        parsed = urlparse(self._resumes_url)
        url = urlunparse((parsed.scheme, parsed.netloc, f"/resume/{resume_id}", "", "", ""))
        page = self._require_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.locator('[data-qa="resume-block-title-position"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
        except PlaywrightTimeoutError as error:
            raise RuntimeError("Страница резюме не загрузилась") from error
        payload = page.evaluate(RESUME_DETAILS_SCRIPT)
        if not isinstance(payload, dict):
            raise RuntimeError("hh.ru вернул некорректные данные резюме")
        raw_blocks = payload.get("experienceBlocks")
        if not isinstance(raw_blocks, list) or not all(
            isinstance(block, dict) for block in raw_blocks
        ):
            raise RuntimeError("hh.ru вернул некорректные блоки опыта")
        experience_blocks = tuple(
            HhResumeExperienceBlock(
                company=self._resume_text(block, "company"),
                position=self._resume_text(block, "position"),
                period=self._resume_text(block, "period"),
                description=self._resume_text(block, "description"),
                text=self._required_resume_text(block, "text", "блока опыта"),
            )
            for block in raw_blocks
        )
        return HhResumeDetails(
            hh_id=resume_id,
            title=self._required_resume_text(payload, "title", "названия резюме"),
            experience=self._resume_text(payload, "experience"),
            skills=self._resume_text(payload, "skills"),
            education=self._resume_text(payload, "education"),
            city=self._resume_text(payload, "city"),
            salary=self._resume_text(payload, "salary"),
            employment=self._resume_text(payload, "employment"),
            work_format=self._resume_text(payload, "workFormat"),
            relocation=self._resume_text(payload, "relocation"),
            business_trips=self._resume_text(payload, "businessTrips"),
            about=self._resume_text(payload, "about"),
            experience_blocks=experience_blocks,
        )

    def read_application_statuses(self) -> tuple[HhNegotiationData, ...]:
        page = self._open_negotiations()
        payload = page.evaluate(NEGOTIATIONS_SCRIPT)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise RuntimeError("hh.ru вернул некорректный список откликов")
        statuses: list[HhNegotiationData] = []
        for position, item in enumerate(payload):
            vacancy_id = self._optional_string(item, "vacancyId")
            if vacancy_id and not vacancy_id.isdigit():
                raise RuntimeError("hh.ru вернул некорректный номер вакансии в истории")
            vacancy_href = self._optional_string(item, "vacancyHref")
            vacancy_id = vacancy_id or self._vacancy_id_from_href(vacancy_href)
            if not vacancy_id:
                if vacancy_href:
                    continue
                vacancy_id = self._vacancy_id_from_negotiation_card(
                    position=position,
                    expected_count=len(payload),
                )
            status_qa = self._optional_string(item, "statusQa").casefold()
            status_label = self._optional_string(item, "statusLabel")
            statuses.append(
                HhNegotiationData(
                    vacancy_id=vacancy_id,
                    status=self._negotiation_status(status_qa, status_label),
                    status_label=status_label or "Отклик отправлен",
                    chat_available=item.get("chatAvailable") is True,
                )
            )
        return tuple(statuses)

    def read_recruiter_messages(
        self,
        vacancy_ids: tuple[str, ...],
    ) -> tuple[HhChatMessageData, ...]:
        selected_ids = {value.strip() for value in vacancy_ids if value.strip()}
        if not selected_ids:
            return ()
        negotiations = {
            item.vacancy_id: item
            for item in self.read_application_statuses()
            if item.chat_available and item.vacancy_id in selected_ids
        }
        messages: list[HhChatMessageData] = []
        for vacancy_id in negotiations:
            page = self._open_negotiations()
            opened = page.evaluate(OPEN_NEGOTIATION_CHAT_SCRIPT, vacancy_id)
            if opened is not True:
                continue
            frame = self._wait_for_chat_frame(page)
            if frame is None:
                continue
            payload = frame.evaluate(CHAT_MESSAGES_SCRIPT, vacancy_id)
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise RuntimeError("hh.ru вернул некорректную переписку")
            for item in payload:
                raw_direction = self._required_string(item, "direction", "направления сообщения")
                try:
                    direction = MessageDirection(raw_direction)
                except ValueError as error:
                    raise RuntimeError("hh.ru вернул неизвестное направление сообщения") from error
                messages.append(
                    HhChatMessageData(
                        vacancy_id=self._required_string(
                            item,
                            "vacancyId",
                            "идентификатора вакансии сообщения",
                        ),
                        hh_id=self._required_string(
                            item,
                            "messageId",
                            "идентификатора сообщения",
                        ),
                        direction=direction,
                        body=self._required_string(item, "body", "текста сообщения"),
                        displayed_time=self._optional_string(item, "displayedTime"),
                    )
                )
            close = page.locator('[data-qa="chatik-close-chatik"]')
            if close.count() == 1:
                close.first.click(no_wait_after=True)
                page.wait_for_timeout(500)
        return tuple(messages)

    def apply_to_vacancy(
        self,
        source_url: str,
        *,
        expected_resume_hh_id: str,
        expected_resume_title: str,
        cover_letter: str,
        submit: bool = False,
        submit_guard: Callable[[], bool] | None = None,
    ) -> HhApplyResult:
        page = self._require_page()
        attempt = _SubmissionAttempt()
        try:
            return self._apply_to_vacancy(
                source_url,
                expected_resume_hh_id=expected_resume_hh_id,
                expected_resume_title=expected_resume_title,
                cover_letter=cover_letter,
                submit=submit,
                submit_guard=submit_guard,
                attempt=attempt,
            )
        except Exception as error:
            if attempt.started:
                return HhApplyResult(
                    HhApplyStatus.UNKNOWN_RESULT,
                    page.url,
                    (f"Ошибка после начала отправки: {type(error).__name__}"),
                )
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                (f"Ошибка до нажатия кнопки отправки: {type(error).__name__}"),
            )

    def _apply_to_vacancy(
        self,
        source_url: str,
        *,
        expected_resume_hh_id: str,
        expected_resume_title: str,
        cover_letter: str,
        submit: bool,
        submit_guard: Callable[[], bool] | None,
        attempt: _SubmissionAttempt,
    ) -> HhApplyResult:
        vacancy_url = self._canonical_vacancy_url(source_url)
        page = self._require_page()
        try:
            initial_response = page.goto(vacancy_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
        if initial_response is not None and initial_response.status == 429:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                retry_after_seconds=self._retry_after_seconds(initial_response),
            )
        if not self.is_authenticated():
            return HhApplyResult(HhApplyStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhApplyResult(HhApplyStatus.CAPTCHA_REQUIRED, page.url)
        response_links = page.locator('[data-qa="vacancy-response-link-top"]:visible')
        if response_links.count() == 0:
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
        try:
            response_links.first.click(no_wait_after=True, timeout=min(self._timeout_ms, 10_000))
            page.locator('[data-qa="resume-title"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
            page.wait_for_timeout(500)
        except PlaywrightError:
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)

        initial = self._application_snapshot(page)
        body_text = initial.body_text
        if not self.is_authenticated():
            return HhApplyResult(HhApplyStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhApplyResult(HhApplyStatus.CAPTCHA_REQUIRED, page.url)
        if self._contains_any(
            body_text,
            "подозрительная активность",
            "аккаунт заблокирован",
            "достигнут лимит откликов",
            "слишком много откликов",
        ):
            return HhApplyResult(HhApplyStatus.ACCOUNT_WARNING, page.url)
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhApplyResult(HhApplyStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhApplyResult(HhApplyStatus.ALREADY_APPLIED, page.url, body_text[:1000])
        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() == 1 and self._contains_any(
            submit_button.first.inner_text(),
            "повторно",
        ):
            return HhApplyResult(HhApplyStatus.ALREADY_APPLIED, page.url, body_text[:1000])

        try:
            initial = self._select_exact_resume(
                page,
                expected_resume_hh_id=expected_resume_hh_id,
                expected_resume_title=expected_resume_title,
            )
        except _ResumeSelectionError as error:
            return HhApplyResult(
                (
                    HhApplyStatus.RETRYABLE_ERROR
                    if error.retryable
                    else HhApplyStatus.RESUME_MISMATCH
                ),
                page.url,
                confirmation=str(error),
                warnings=initial.warnings,
            )
        body_text = initial.body_text
        if not self.is_authenticated():
            return HhApplyResult(HhApplyStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhApplyResult(HhApplyStatus.CAPTCHA_REQUIRED, page.url)
        if self._contains_any(
            body_text,
            "подозрительная активность",
            "аккаунт заблокирован",
            "достигнут лимит откликов",
            "слишком много откликов",
        ):
            return HhApplyResult(HhApplyStatus.ACCOUNT_WARNING, page.url)
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhApplyResult(HhApplyStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhApplyResult(HhApplyStatus.ALREADY_APPLIED, page.url, body_text[:1000])
        if initial.questions:
            return HhApplyResult(
                HhApplyStatus.QUESTIONS_REQUIRED,
                page.url,
                questions=initial.questions,
                warnings=initial.warnings,
                screening_form=initial.screening_form,
            )

        if cover_letter.strip():
            letter = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
            if letter.count() == 0:
                toggle = page.locator('[data-qa="vacancy-response-letter-toggle"]')
                if toggle.count() == 0:
                    toggle = page.locator('[data-qa="add-cover-letter"]')
                if toggle.count() != 1:
                    return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
                toggle.click()
            letter.first.wait_for(state="visible", timeout=self._timeout_ms)
            letter.first.fill(cover_letter.strip())

        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() != 1 or not submit_button.first.is_enabled():
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
        if not submit or submit_guard is None:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Форма заполнена и оставлена открытой без отправки",
                warnings=initial.warnings,
            )

        final = self._application_snapshot(page)
        if self._normalized_ui_text(final.resume_title) != self._normalized_ui_text(
            expected_resume_title
        ):
            return HhApplyResult(
                HhApplyStatus.RESUME_MISMATCH,
                page.url,
                (
                    f"Перед отправкой ожидалось резюме «{expected_resume_title}», "
                    f"выбрано «{final.resume_title}»"
                ),
                warnings=final.warnings,
            )
        if final.questions:
            return HhApplyResult(
                HhApplyStatus.QUESTIONS_REQUIRED,
                page.url,
                questions=final.questions,
                warnings=final.warnings,
                screening_form=final.screening_form,
            )
        if not self.is_authenticated():
            return HhApplyResult(HhApplyStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhApplyResult(HhApplyStatus.CAPTCHA_REQUIRED, page.url)
        if self._contains_any(
            final.body_text,
            "подозрительная активность",
            "аккаунт заблокирован",
            "достигнут лимит откликов",
            "слишком много откликов",
        ):
            return HhApplyResult(HhApplyStatus.ACCOUNT_WARNING, page.url)
        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() != 1 or not submit_button.first.is_enabled():
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
        try:
            submission_allowed = submit_guard()
        except Exception as error:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                (f"Не удалось повторно проверить данные перед отправкой: {type(error).__name__}"),
                warnings=final.warnings,
            )
        if not submission_allowed:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Перед отправкой изменились проверенные данные; кнопка не нажата",
                warnings=final.warnings,
            )

        vacancy_id, _normalized_url = self._vacancy_id_and_url(source_url)
        parsed = urlparse(self._resumes_url)
        response: Response | None = None
        submit_button.first.click(
            trial=True,
            timeout=min(self._timeout_ms, 10_000),
        )
        try:
            with page.expect_response(
                self._is_application_submission_response,
                timeout=self._timeout_ms,
            ) as response_info:
                attempt.started = True
                submit_button.first.click(no_wait_after=True)
            response = response_info.value
            page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as error:
            return HhApplyResult(
                HhApplyStatus.UNKNOWN_RESULT,
                page.url,
                f"После нажатия hh.ru не подтвердил результат: {type(error).__name__}",
                warnings=initial.warnings,
            )

        confirmation = self._application_confirmation(page, response)
        if confirmation:
            return HhApplyResult(
                HhApplyStatus.APPLIED,
                page.url,
                confirmation,
                warnings=initial.warnings,
            )
        if self._vacancy_in_negotiations(
            page,
            parsed.scheme,
            parsed.netloc,
            vacancy_id,
        ):
            return HhApplyResult(
                HhApplyStatus.APPLIED,
                page.url,
                "Отклик найден в истории hh.ru",
                warnings=initial.warnings,
            )
        return HhApplyResult(
            HhApplyStatus.UNKNOWN_RESULT,
            page.url,
            "Кнопка нажата один раз, но hh.ru не подтвердил результат",
            warnings=initial.warnings,
        )

    def open_screening_form(
        self,
        source_url: str,
        *,
        expected_resume_hh_id: str,
        expected_resume_title: str,
        expected_version_hash: str,
        answers: dict[str, str],
        cover_letter: str = "",
    ) -> HhFormReviewResult:
        page = self._require_page()
        vacancy_url = self._canonical_vacancy_url(source_url)
        try:
            response = page.goto(
                vacancy_url,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            return HhFormReviewResult(HhFormReviewStatus.UNAVAILABLE, page.url)
        if response is not None and response.status == 429:
            return HhFormReviewResult(
                HhFormReviewStatus.UNAVAILABLE,
                page.url,
                message="hh.ru временно ограничил обращения",
            )
        if not self.is_authenticated():
            return HhFormReviewResult(HhFormReviewStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhFormReviewResult(HhFormReviewStatus.CAPTCHA_REQUIRED, page.url)
        response_links = page.locator('[data-qa="vacancy-response-link-top"]:visible')
        if response_links.count() == 0:
            return HhFormReviewResult(
                HhFormReviewStatus.UNAVAILABLE,
                page.url,
                message="hh.ru не показал кнопку отклика",
            )
        try:
            response_links.first.click(no_wait_after=True, timeout=min(self._timeout_ms, 10_000))
            page.locator('[data-qa="resume-title"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
            page.wait_for_timeout(500)
        except PlaywrightError:
            return HhFormReviewResult(
                HhFormReviewStatus.UNAVAILABLE,
                page.url,
                message="Форма отклика hh.ru не открылась",
            )

        snapshot = self._application_snapshot(page)
        body_text = snapshot.body_text
        if not self.is_authenticated():
            return HhFormReviewResult(HhFormReviewStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhFormReviewResult(HhFormReviewStatus.CAPTCHA_REQUIRED, page.url)
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhFormReviewResult(HhFormReviewStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhFormReviewResult(HhFormReviewStatus.ALREADY_APPLIED, page.url)

        try:
            snapshot = self._select_exact_resume(
                page,
                expected_resume_hh_id=expected_resume_hh_id,
                expected_resume_title=expected_resume_title,
            )
        except _ResumeSelectionError as error:
            return HhFormReviewResult(
                (
                    HhFormReviewStatus.UNAVAILABLE
                    if error.retryable
                    else HhFormReviewStatus.RESUME_MISMATCH
                ),
                page.url,
                current_form=snapshot.screening_form,
                message=str(error),
            )
        body_text = snapshot.body_text
        if not self.is_authenticated():
            return HhFormReviewResult(HhFormReviewStatus.AUTH_REQUIRED, page.url)
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return HhFormReviewResult(HhFormReviewStatus.CAPTCHA_REQUIRED, page.url)
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhFormReviewResult(HhFormReviewStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhFormReviewResult(HhFormReviewStatus.ALREADY_APPLIED, page.url)
        if not snapshot.screening_form.fields:
            return HhFormReviewResult(
                HhFormReviewStatus.UNAVAILABLE,
                page.url,
                current_form=snapshot.screening_form,
                message="Анкета работодателя не найдена",
            )
        if screening_form_hash(snapshot.screening_form) != expected_version_hash:
            return HhFormReviewResult(
                HhFormReviewStatus.FORM_CHANGED,
                page.url,
                current_form=snapshot.screening_form,
                message="Состав анкеты изменился; старые ответы не подставлены",
            )

        if cover_letter.strip():
            letter = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
            if letter.count() == 0:
                toggle = page.locator('[data-qa="vacancy-response-letter-toggle"]')
                if toggle.count() == 0:
                    toggle = page.locator('[data-qa="add-cover-letter"]')
                if toggle.count() == 1:
                    toggle.click()
                    letter = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
            if letter.count() == 1:
                letter.first.wait_for(state="visible", timeout=self._timeout_ms)
                letter.first.fill(cover_letter.strip())

        payload = [{"key": key, "value": value} for key, value in answers.items() if value.strip()]
        fill_result = page.evaluate(FILL_APPLICATION_FORM_SCRIPT, payload)
        if not isinstance(fill_result, dict):
            raise RuntimeError("hh.ru вернул некорректный результат заполнения анкеты")
        raw_filled = fill_result.get("filled", [])
        raw_skipped = fill_result.get("skipped", [])
        if not isinstance(raw_filled, list) or not all(
            isinstance(value, str) for value in raw_filled
        ):
            raise RuntimeError("hh.ru вернул некорректный список заполненных полей")
        if not isinstance(raw_skipped, list) or not all(
            isinstance(value, str) for value in raw_skipped
        ):
            raise RuntimeError("hh.ru вернул некорректный список пропущенных полей")
        return HhFormReviewResult(
            HhFormReviewStatus.READY,
            page.url,
            current_form=snapshot.screening_form,
            filled_keys=tuple(raw_filled),
            skipped_keys=tuple(raw_skipped),
            message="Анкета заполнена, но не отправлена",
        )

    def send_recruiter_message(
        self,
        source_url: str,
        body: str,
    ) -> MessageSendResult:
        exact_body = body.strip()
        if not exact_body:
            raise ValueError("Текст сообщения не может быть пустым")
        vacancy_id, _normalized_url = self._vacancy_id_and_url(source_url)
        try:
            page = self._open_negotiations()
        except (HhSyncBlockedError, RuntimeError):
            return MessageSendResult(MessageSendOutcome.FAILED)
        try:
            opened = page.evaluate(OPEN_NEGOTIATION_CHAT_SCRIPT, vacancy_id)
        except PlaywrightError:
            return MessageSendResult(MessageSendOutcome.FAILED)
        if opened is not True:
            return MessageSendResult(MessageSendOutcome.FAILED)
        frame = self._wait_for_chat_frame(page)
        if frame is None:
            return MessageSendResult(MessageSendOutcome.FAILED)

        editor = frame.locator('[data-qa="chatik-new-message-text"]')
        submit = frame.locator('[data-qa="chatik-do-send-message"]')
        if (
            editor.count() != 1
            or submit.count() != 1
            or not editor.first.is_enabled()
            or not submit.first.is_enabled()
        ):
            return MessageSendResult(MessageSendOutcome.FAILED)
        editor.first.fill(exact_body)

        response: Response | None = None
        try:
            with page.expect_response(
                self._is_message_submission_response,
                timeout=self._timeout_ms,
            ) as response_info:
                submit.first.click(no_wait_after=True)
            response = response_info.value
            page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError:
            return MessageSendResult(MessageSendOutcome.UNKNOWN_RESULT)

        if self._message_visible(frame, exact_body):
            return MessageSendResult(
                MessageSendOutcome.SENT,
                self._message_external_id(response),
            )
        if response is not None and 400 <= response.status < 500:
            return MessageSendResult(MessageSendOutcome.FAILED)
        return MessageSendResult(MessageSendOutcome.UNKNOWN_RESULT)

    @staticmethod
    def _retry_after_seconds(response: Response) -> int | None:
        try:
            value = response.header_value("retry-after")
        except PlaywrightError:
            return None
        if not value:
            return None
        stripped = value.strip()
        if stripped.isdigit():
            return min(int(stripped), 86_400)
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return min(max(ceil((retry_at - datetime.now(UTC)).total_seconds()), 0), 86_400)

    def _vacancy_in_negotiations(
        self,
        page: Page,
        scheme: str,
        netloc: str,
        vacancy_id: str,
    ) -> bool:
        negotiations_url = urlunparse((scheme, netloc, "/applicant/negotiations", "", "", ""))
        try:
            page.goto(negotiations_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
            payload = page.evaluate(NEGOTIATIONS_SCRIPT)
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                return False
            for position, item in enumerate(payload):
                react_id = self._optional_string(item, "vacancyId")
                if react_id and not react_id.isdigit():
                    return False
                direct_id = self._vacancy_id_from_href(self._optional_string(item, "vacancyHref"))
                current_id = (
                    react_id
                    or direct_id
                    or self._vacancy_id_from_negotiation_card(
                        position=position,
                        expected_count=len(payload),
                    )
                )
                if current_id == vacancy_id:
                    return True
            return False
        except (PlaywrightError, RuntimeError):
            return False

    def _open_negotiations(self) -> Page:
        page = self._require_page()
        parsed = urlparse(self._resumes_url)
        url = urlunparse((parsed.scheme, parsed.netloc, "/applicant/negotiations", "", "", ""))
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
        except PlaywrightError as error:
            raise RuntimeError("Страница откликов hh.ru не загрузилась") from error
        if not self.is_authenticated():
            raise HhSyncBlockedError("AUTH_REQUIRED", "Требуется повторный вход в hh.ru")
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            raise HhSyncBlockedError("CAPTCHA_REQUIRED", "hh.ru запросил проверку")
        body_text = page.locator("body").inner_text()
        if self._contains_any(
            body_text,
            "подозрительная активность",
            "аккаунт заблокирован",
            "слишком много запросов",
        ):
            raise HhSyncBlockedError(
                "ACCOUNT_WARNING",
                "hh.ru показал предупреждение аккаунта",
            )
        return page

    def _vacancy_id_from_negotiation_card(
        self,
        *,
        position: int,
        expected_count: int,
    ) -> str:
        page = self._open_negotiations()
        cards = page.locator('[data-qa="negotiations-item"]')
        if cards.count() != expected_count:
            raise RuntimeError(
                "Список откликов hh.ru изменился во время чтения; сверка остановлена"
            )
        items = cards.all()
        if position < 0 or position >= len(items):
            raise RuntimeError("hh.ru вернул некорректную позицию отклика")
        try:
            items[position].click(
                no_wait_after=True,
                timeout=min(self._timeout_ms, 10_000),
            )
            page.wait_for_timeout(1_500)
        except PlaywrightError as error:
            raise RuntimeError("Не удалось открыть отклик из истории hh.ru") from error
        vacancy_id = self._vacancy_id_from_href(page.url)
        if not vacancy_id:
            raise RuntimeError("hh.ru не показал номер вакансии после открытия отклика")
        return vacancy_id

    def _wait_for_chat_frame(self, page: Page) -> Frame | None:
        attempts = max(min(self._timeout_ms // 500, 20), 1)
        for _attempt in range(attempts):
            frame = next(
                (candidate for candidate in page.frames if "chatik.hh.ru/chat/" in candidate.url),
                None,
            )
            if frame is not None:
                return frame
            page.wait_for_timeout(500)
        return None

    @staticmethod
    def _negotiation_status(status_qa: str, status_label: str) -> HhNegotiationStatus:
        folded = f"{status_qa} {status_label}".casefold()
        if "discard" in folded or "отказ" in folded:
            return HhNegotiationStatus.REJECTED
        if (
            "interview" in folded
            or "invitation" in folded
            or "собеседован" in folded
            or "приглашен" in folded
        ):
            return HhNegotiationStatus.INVITED
        if "closed" in folded or "архив" in folded:
            return HhNegotiationStatus.CLOSED
        if "not-viewed" in folded or "не просмотрен" in folded:
            return HhNegotiationStatus.APPLIED
        if "viewed" in folded or "просмотрен" in folded:
            return HhNegotiationStatus.VIEWED
        return HhNegotiationStatus.APPLIED

    @staticmethod
    def _is_application_submission_response(response: Response) -> bool:
        try:
            method = response.request.method.upper()
            parsed = urlparse(response.url)
        except (AttributeError, ValueError):
            return False
        return (
            method == "POST"
            and (parsed.hostname == "hh.ru" or (parsed.hostname or "").endswith(".hh.ru"))
            and "vacancy_response" in parsed.path
        )

    @staticmethod
    def _is_message_submission_response(response: Response) -> bool:
        try:
            method = response.request.method.upper()
            parsed = urlparse(response.url)
        except (AttributeError, ValueError):
            return False
        path = parsed.path.casefold()
        return (
            method == "POST"
            and (parsed.hostname == "hh.ru" or (parsed.hostname or "").endswith(".hh.ru"))
            and ("message" in path or "chat" in path)
        )

    def _application_confirmation(
        self,
        page: Page,
        response: Response | None,
    ) -> str:
        body_text = ""
        with suppress(PlaywrightError):
            body_text = page.locator("body").inner_text()
        if self._contains_any(
            body_text,
            "отклик отправлен",
            "отклик успешно отправлен",
            "вы откликнулись",
            "отклик принят",
        ):
            return "hh.ru подтвердил отправку отклика"
        if response is None or not 200 <= response.status < 300:
            return ""
        try:
            response_text = response.text()
        except PlaywrightError:
            return ""
        compact = re.sub(r"\s+", "", response_text).casefold()
        if (
            '"success":true' in compact
            or '"status":"success"' in compact
            or '"result":"success"' in compact
        ):
            return "hh.ru подтвердил отправку отклика"
        return ""

    @staticmethod
    def _message_visible(page: Page | Frame, body: str) -> bool:
        try:
            page_text = page.locator("body").inner_text()
        except PlaywrightError:
            return False
        return " ".join(body.split()) in " ".join(page_text.split())

    @staticmethod
    def _message_external_id(response: Response | None) -> str | None:
        if response is None:
            return None
        try:
            payload = json.loads(response.text())
        except (json.JSONDecodeError, PlaywrightError):
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get("id") or payload.get("message_id")
        return str(value)[:128] if isinstance(value, (int, str)) and str(value).strip() else None

    def is_authenticated(self) -> bool:
        page = self._require_page()
        parsed_url = urlparse(page.url)
        hostname = parsed_url.hostname or ""
        is_hh = hostname == "hh.ru" or hostname.endswith(".hh.ru")
        return is_hh and "/account/login" not in parsed_url.path

    def submit_credentials(self, credentials: HhCredentials) -> LoginStatus:
        page = self._require_page()
        self._open_applicant_form(page)
        self._fill_login(page, credentials.login.strip())
        self._click_unique(page.locator('[data-qa="expand-login-by-password"]'))

        password = page.locator(
            '[data-qa="applicant-login-input-password"], '
            '[data-qa="account-login-password"], input[name="password"]'
        )
        try:
            password.wait_for(state="visible", timeout=self._timeout_ms)
        except PlaywrightTimeoutError:
            return self._classify(page)

        password.fill(credentials.password)
        self._click_unique(page.locator('[data-qa="submit-button"]'))
        page.wait_for_timeout(1_000)
        return self._classify(page)

    def _open_applicant_form(self, page: Page) -> None:
        account_card = page.locator('[data-qa="applicant-login-card"]')
        if account_card.count() == 0:
            return
        account_type = page.locator('[data-qa^="account-type-card-APPLICANT"]')
        if account_type.count() == 1 and not account_type.is_checked():
            account_type.check(force=True)
        if page.locator('[data-qa="expand-login-by-password"]').count() == 0:
            self._click_unique(
                page.locator('[data-qa="submit-button"]'),
                no_wait_after=True,
            )
            page.locator('[data-qa="expand-login-by-password"]').wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )

    def _fill_login(self, page: Page, login: str) -> None:
        if "@" in login:
            email_type = page.locator('[data-qa^="credential-type-EMAIL"]')
            if email_type.count() == 1 and not email_type.is_checked():
                email_type.check(force=True)
            page.locator('[data-qa="applicant-login-input-email"]').fill(login)
            return

        phone_type = page.locator('[data-qa^="credential-type-PHONE"]')
        if phone_type.count() == 1 and not phone_type.is_checked():
            phone_type.check(force=True)
        digits = "".join(character for character in login if character.isdigit())
        if len(digits) == 11 and digits[0] in {"7", "8"}:
            digits = digits[1:]
        page.locator('[data-qa="magritte-phone-input-national-number-input"]').fill(digits)

    def _classify(self, page: Page) -> LoginStatus:
        if self.is_authenticated():
            return LoginStatus.AUTHENTICATED
        if self._any_visible(page, '[data-qa*="captcha"], iframe[src*="captcha"]'):
            return LoginStatus.CAPTCHA_REQUIRED
        if self._any_visible(
            page,
            '[data-qa*="otp"], [data-qa*="verification-code"], input[name*="code"]',
        ):
            return LoginStatus.CONFIRMATION_REQUIRED
        if self._any_visible(page, '[data-qa="form-helper-error"]'):
            return LoginStatus.INVALID_CREDENTIALS
        return LoginStatus.MANUAL_ACTION_REQUIRED

    @staticmethod
    def _any_visible(page: Page, selector: str) -> bool:
        locators = page.locator(selector)
        return any(locator.is_visible() for locator in locators.all())

    @staticmethod
    def _click_unique(locator: Locator, *, no_wait_after: bool = False) -> None:
        count = locator.count()
        if count != 1:
            raise RuntimeError(f"Ожидался один элемент hh.ru, найдено: {count}")
        locator.click(no_wait_after=no_wait_after)

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Браузер не запущен")
        return self._page

    def _select_exact_resume(
        self,
        page: Page,
        *,
        expected_resume_hh_id: str,
        expected_resume_title: str,
    ) -> _ApplicationSnapshot:
        resume_hh_id = expected_resume_hh_id.strip()
        resume_title = expected_resume_title.strip()
        if not resume_hh_id or not resume_title:
            raise _ResumeSelectionError(
                "У назначенного резюме отсутствует номер или название",
                retryable=False,
            )

        selected_card = page.locator('[data-qa="resume-title"]')
        if selected_card.count() != 1:
            raise _ResumeSelectionError(
                "hh.ru не показал единственную карточку выбранного резюме",
                retryable=True,
            )
        try:
            selected_card.first.click(no_wait_after=True, timeout=min(self._timeout_ms, 10_000))
        except PlaywrightError as error:
            raise _ResumeSelectionError(
                "hh.ru не открыл список резюме для отклика",
                retryable=True,
            ) from error

        bottom_sheet_selector = '[data-qa="bottom-sheet-content"]:visible input[name="resumeId"]'
        dropdown_selector = '[data-qa="drop-base"]:visible [role="option"]'
        options: list[Locator] = []
        uses_bottom_sheet = False
        for _attempt in range(10):
            page.wait_for_timeout(500)
            options = page.locator(bottom_sheet_selector).all()
            uses_bottom_sheet = bool(options)
            if not options:
                options = page.locator(dropdown_selector).all()
            if options:
                break
        option_values = [option.get_attribute("value") or "" for option in options]
        if not uses_bottom_sheet and options:
            option_values = []
            prefix = "magritte-select-option-"
            for option in options:
                data_qa = option.get_attribute("data-qa") or ""
                option_values.append(
                    data_qa.removeprefix(prefix) if data_qa.startswith(prefix) else ""
                )
        if not option_values:
            raise _ResumeSelectionError(
                "Список резюме hh.ru не успел загрузиться; отклик остановлен",
                retryable=True,
            )
        if any(not value for value in option_values) or len(set(option_values)) != len(
            option_values
        ):
            raise _ResumeSelectionError(
                "hh.ru показал неоднозначный список резюме; отклик остановлен",
                retryable=False,
            )
        matching = [
            option
            for option, value in zip(options, option_values, strict=True)
            if value == resume_hh_id
        ]
        if len(matching) != 1:
            raise _ResumeSelectionError(
                f"Резюме «{resume_title}» с точным номером не найдено в форме hh.ru",
                retryable=False,
            )
        try:
            matching[0].click(
                force=uses_bottom_sheet,
                no_wait_after=True,
                timeout=min(self._timeout_ms, 10_000),
            )
            for _attempt in range(10):
                page.wait_for_timeout(500)
                if (
                    page.locator(bottom_sheet_selector).count() == 0
                    and page.locator(dropdown_selector).count() == 0
                ):
                    break
            else:
                page.keyboard.press("Escape")
                for _attempt in range(4):
                    page.wait_for_timeout(500)
                    if (
                        page.locator(bottom_sheet_selector).count() == 0
                        and page.locator(dropdown_selector).count() == 0
                    ):
                        break
                else:
                    selected_card.first.click(
                        no_wait_after=True,
                        timeout=min(self._timeout_ms, 10_000),
                    )
                    for _attempt in range(4):
                        page.wait_for_timeout(500)
                        if (
                            page.locator(bottom_sheet_selector).count() == 0
                            and page.locator(dropdown_selector).count() == 0
                        ):
                            break
                    else:
                        raise _ResumeSelectionError(
                            "Список резюме не закрылся после выбора; отклик остановлен",
                            retryable=True,
                        )
        except PlaywrightError as error:
            raise _ResumeSelectionError(
                "hh.ru не подтвердил выбор назначенного резюме",
                retryable=True,
            ) from error

        snapshot = self._application_snapshot(page)
        if self._normalized_ui_text(snapshot.resume_title) != self._normalized_ui_text(
            resume_title
        ):
            raise _ResumeSelectionError(
                (
                    f"Ожидалось резюме «{resume_title}», "
                    f"после выбора показано «{snapshot.resume_title}»"
                ),
                retryable=False,
            )
        return snapshot

    @staticmethod
    def _normalized_ui_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).replace("\N{NO-BREAK SPACE}", " ")
        return " ".join(normalized.split()).casefold()

    def _application_snapshot(self, page: Page) -> _ApplicationSnapshot:
        payload = page.evaluate(APPLICATION_FORM_SCRIPT)
        if not isinstance(payload, dict):
            raise RuntimeError("hh.ru вернул некорректную форму отклика")
        raw_fields = payload.get("fields")
        raw_questions = payload.get("questions")
        raw_warnings = payload.get("warnings")
        if (
            raw_fields is None
            and isinstance(raw_questions, list)
            and all(isinstance(item, str) for item in raw_questions)
        ):
            raw_fields = [
                {
                    "key": f"question:{position}:{question.casefold()[:220]}",
                    "question": question,
                    "fieldType": "unknown",
                    "isRequired": True,
                    "options": [],
                    "maxLength": None,
                }
                for position, question in enumerate(raw_questions)
            ]
        if not isinstance(raw_fields, list) or not all(
            isinstance(item, dict) for item in raw_fields
        ):
            raise RuntimeError("hh.ru вернул некорректные вопросы работодателя")
        if not isinstance(raw_warnings, list) or not all(
            isinstance(item, str) for item in raw_warnings
        ):
            raise RuntimeError("hh.ru вернул некорректные предупреждения")
        fields: list[HhScreeningField] = []
        for raw_field in raw_fields:
            raw_options = raw_field.get("options", [])
            if not isinstance(raw_options, list) or not all(
                isinstance(option, str) for option in raw_options
            ):
                raise RuntimeError("hh.ru вернул некорректные варианты ответа")
            raw_max_length = raw_field.get("maxLength")
            if raw_max_length is not None and not isinstance(raw_max_length, int):
                raise RuntimeError("hh.ru вернул некорректное ограничение длины")
            fields.append(
                HhScreeningField(
                    key=self._required_string(raw_field, "key", "ключа вопроса"),
                    question=self._required_string(raw_field, "question", "текста вопроса"),
                    field_type=self._required_string(raw_field, "fieldType", "типа вопроса"),
                    is_required=raw_field.get("isRequired") is True,
                    options=tuple(option.strip() for option in raw_options if option.strip()),
                    max_length=raw_max_length,
                    format_hint=self._optional_string(raw_field, "formatHint"),
                    has_attachment=raw_field.get("hasAttachment") is True,
                    has_external_action=raw_field.get("hasExternalAction") is True,
                    has_test_assignment=raw_field.get("hasTestAssignment") is True,
                )
            )
        screening_form = HhScreeningForm(
            fields=tuple(fields),
            warnings=tuple(item.strip() for item in raw_warnings if item.strip()),
        )
        return _ApplicationSnapshot(
            screening_form=screening_form,
            resume_title=self._optional_string(payload, "resumeTitle"),
            body_text=self._optional_string(payload, "bodyText"),
        )

    def _application_response_url(self, source_url: str) -> str:
        vacancy_id, normalized_url = self._vacancy_id_and_url(source_url)
        parsed = urlparse(normalized_url)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                "/applicant/vacancy_response",
                "",
                urlencode(
                    {
                        "vacancyId": vacancy_id,
                        "startedWithQuestion": "false",
                        "hhtmFrom": "vacancy",
                    }
                ),
                "",
            )
        )

    def _canonical_vacancy_url(self, source_url: str) -> str:
        vacancy_id, _normalized_url = self._vacancy_id_and_url(source_url)
        base = urlparse(self._resumes_url)
        return urlunparse(
            (
                base.scheme,
                base.netloc,
                f"/vacancy/{vacancy_id}",
                "",
                "",
                "",
            )
        )

    @staticmethod
    def _required_string(payload: dict[object, object], key: str, label: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Данные {label} отсутствуют на странице hh.ru")
        return value.strip()

    @staticmethod
    def _optional_string(payload: dict[object, object], key: str) -> str:
        value = payload.get(key)
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _required_resume_text(
        cls,
        payload: dict[object, object],
        key: str,
        label: str,
    ) -> str:
        value = cls._resume_text(payload, key)
        if not value:
            raise RuntimeError(f"Данные {label} отсутствуют на странице hh.ru")
        return value

    @staticmethod
    def _resume_text(payload: dict[object, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            return ""
        action_lines = {
            "развернуть",
            "свернуть",
            "добавить",
            "редактировать",
            "указать уровень",
            "указать уровни",
        }
        result: list[str] = []
        for raw_line in value.replace("\u00a0", " ").splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if line.casefold() in action_lines:
                continue
            if line:
                result.append(line)
            elif result and result[-1]:
                result.append("")
        return "\n".join(result).strip()

    @staticmethod
    def _date_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError("hh.ru вернул некорректную дату вакансии") from error
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @staticmethod
    def _salary(value: str) -> tuple[Decimal | None, Decimal | None, str | None, bool | None]:
        if not value:
            return None, None, None, None
        normalized = re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()
        amounts = [
            Decimal(re.sub(r"\D", "", match))
            for match in re.findall(r"\d[\d\s]*", normalized)
            if re.sub(r"\D", "", match)
        ]
        salary_from: Decimal | None = None
        salary_to: Decimal | None = None
        if len(amounts) >= 2:
            salary_from, salary_to = amounts[0], amounts[1]
        elif amounts:
            if re.search(r"\bдо\s+\d", normalized, re.IGNORECASE):
                salary_to = amounts[0]
            else:
                salary_from = amounts[0]
        folded = normalized.casefold()
        currency = None
        currencies = (("₽", "RUR"), ("руб", "RUR"), ("$", "USD"), ("€", "EUR"), ("₸", "KZT"))
        for marker, code in currencies:
            if marker in folded:
                currency = code
                break
        gross = None
        if "на руки" in folded:
            gross = False
        elif "до вычета" in folded:
            gross = True
        return salary_from, salary_to, currency, gross

    @staticmethod
    def _description_sections(description: str) -> tuple[str | None, str | None, str | None]:
        if not description:
            return None, None, None
        groups: dict[str, list[str]] = {"responsibilities": [], "required": [], "preferred": []}
        current: str | None = None
        headings = (
            (
                "responsibilities",
                ("обязанности", "задачи", "что предстоит", "чем предстоит заниматься"),
            ),
            (
                "required",
                ("требования", "мы ожидаем", "что требуется", "что ждём", "нам важно"),
            ),
            ("preferred", ("будет плюсом", "желательно", "преимуществом будет")),
        )
        for raw_line in description.splitlines():
            line = raw_line.strip(" \t•-–—")
            if not line:
                continue
            folded = line.casefold().rstrip(":")
            matched = next(
                (
                    name
                    for name, markers in headings
                    if any(folded.startswith(marker) for marker in markers)
                ),
                None,
            )
            if matched is not None:
                current = matched
                continue
            if current is not None:
                groups[current].append(line)
        return (
            "\n".join(groups["responsibilities"]) or None,
            "\n".join(groups["required"]) or None,
            "\n".join(groups["preferred"]) or None,
        )

    @staticmethod
    def _contains_any(text: str, *needles: str) -> bool:
        normalized = text.casefold()
        return any(needle.casefold() in normalized for needle in needles)

    @staticmethod
    def _response_confirmation(status: int, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        return f"HTTP {status}: {compact[:900]}"

    @staticmethod
    def _resume_id(href: str) -> str:
        parsed = urlparse(href)
        hostname = parsed.hostname or ""
        if hostname != "hh.ru" and not hostname.endswith(".hh.ru"):
            raise RuntimeError("Ссылка на резюме ведёт за пределы сайта hh.ru")
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "resume" or not parts[1]:
            raise RuntimeError("Идентификатор резюме hh.ru отсутствует")
        return parts[1]

    @staticmethod
    def _vacancy_id_from_href(href: str | None) -> str:
        if not href:
            return ""
        parsed = urlparse(href)
        hostname = parsed.hostname or ""
        if hostname and hostname != "hh.ru" and not hostname.endswith(".hh.ru"):
            return ""
        parts = parsed.path.strip("/").split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "vacancy" else ""

    @staticmethod
    def _search_filters(filters: dict[str, object]) -> list[tuple[str, str]]:
        parameters: list[tuple[str, str]] = []
        for key, value in filters.items():
            if key not in ALLOWED_SEARCH_FILTERS:
                raise ValueError(f"Фильтр поиска hh.ru не поддерживается: {key}")
            values = value if isinstance(value, list | tuple) else [value]
            for item in values:
                if isinstance(item, bool):
                    parameters.append((key, str(item).lower()))
                elif isinstance(item, str | int | float):
                    parameters.append((key, str(item)))
                else:
                    raise ValueError(f"Некорректное значение фильтра hh.ru: {key}")
        return parameters

    @staticmethod
    def _found_vacancies(header: str, *, has_items: bool) -> int:
        match = re.search(r"Найден[^\s]*\s+([\d\s\u00a0]+)\s+ваканс", header, re.IGNORECASE)
        if match is not None:
            return int(re.sub(r"\D", "", match.group(1)))
        if not has_items:
            return 0
        raise RuntimeError("Количество найденных вакансий отсутствует на странице hh.ru")

    @staticmethod
    def _vacancy_id_and_url(href: str) -> tuple[str, str]:
        parsed = urlparse(href)
        hostname = parsed.hostname or ""
        if hostname != "hh.ru" and not hostname.endswith(".hh.ru"):
            raise RuntimeError("Ссылка на вакансию ведёт за пределы сайта hh.ru")
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "vacancy" or not parts[1]:
            raise RuntimeError("Идентификатор вакансии hh.ru отсутствует")
        source_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return parts[1], source_url
