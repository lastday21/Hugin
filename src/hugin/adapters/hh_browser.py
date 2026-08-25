from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import unicodedata
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from ipaddress import IPv4Address
from math import ceil
from pathlib import Path
from time import monotonic, sleep
from types import TracebackType
from typing import BinaryIO
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from playwright.sync_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from hugin.core.network import usable_source_ipv4
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
    HhScreeningSubmission,
    screening_form_hash,
)
from hugin.domain.hh_sync import (
    HhChatMessageData,
    HhNegotiationData,
    HhNegotiationStatus,
    HhSyncBlockedError,
    HhSyncRetryableError,
)
from hugin.domain.vacancies import (
    VacancyAvailability,
    VacancyData,
    VacancySearchResult,
    VacancyUnavailableError,
)
from hugin.services.hh_login import HhCredentials, LoginStatus

_PROFILE_LOCK_FILENAME = ".hugin-browser.lock"
_PROFILE_LOCK_TIMEOUT_SECONDS = 180.0
_PROFILE_LOCK_RETRY_SECONDS = 0.25
_RESUME_OPTIONS_TIMEOUT_MS = 10_000
_WINDOW_LIVENESS_SCRIPT = "() => true"
_HH_HTTPS_PORT = 443
_HH_PROXY_HOST = "127.0.0.1"
_HH_PROXY_ALLOWED_SUFFIXES = (".hh.ru", ".hhcdn.ru")
_HH_DOH_URL = "https://cloudflare-dns.com/dns-query"
_CAPTCHA_SELECTOR = '[data-qa*="captcha"], iframe[src*="captcha"]'
_CONFIRMATION_CODE_SELECTOR = (
    '[data-qa*="otp"], [data-qa*="verification-code"], input[name*="code"]'
)
_AUTHENTICATED_APPLICANT_SELECTOR = (
    '[data-qa="mainmenu_applicantProfile"], '
    '[data-qa="mainmenu_myResumes"], '
    '[data-qa="mainmenu_vacancyResponses"], '
    '[data-qa="mainmenu_negotiations"]'
)
_LOGIN_SURFACE_SELECTORS = (
    '[data-qa="applicant-login-card"]',
    '[data-qa="magritte-phone-input-national-number-input"]',
    '[data-qa="applicant-login-input-email"]',
    '[data-qa="expand-login-by-password"]',
    '[data-qa="applicant-login-input-password"]',
    '[data-qa="account-login-password"]',
    'input[name="password"]',
)
_ACCOUNT_WARNING_MARKERS = (
    "подозрительная активность",
    "аккаунт заблокирован",
    "доступ к аккаунту ограничен",
    "подтвердите, что аккаунт принадлежит вам",
)
_TEMPORARY_REQUEST_LIMIT_MARKERS = (
    "слишком много запросов",
    "слишком много обращений",
    "временно ограничил обращения",
)
_APPLICATION_LIMIT_MARKERS = (
    "достигнут лимит откликов",
    "слишком много откликов",
)
_TEMPORARY_NAVIGATION_ERROR_MARKERS = (
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
)
_NETWORK_RETRY_SECONDS = 60
_TEMPORARY_REQUEST_RETRY_SECONDS = 15 * 60
_APPLICATION_LIMIT_RETRY_SECONDS = 24 * 60 * 60
_SUBMISSION_RESPONSE_TIMEOUT_MS = 10_000
_DANGEROUS_SCREENING_QUESTION = re.compile(
    r"паспорт|passport|банк|bank|банковск|карт[аы]|card|снилс|\bинн\b|полис|"
    r"удостоверен|код\s+(?:из|подтверждения)|смс|sms|otp|оплат|payment|"
    r"перевод.*денег|документ|document|биометр|biometric|медицин|medical|"
    r"здоров|health|диагноз|diagnosis|судим|criminal|установ.*программ|"
    r"испытательн|тестов.*задан|видео",
    re.IGNORECASE,
)
_FORM_ATTACHMENT_WARNING = "Форма содержит загрузку файла"
_FORM_EXTERNAL_LINK_WARNING = "Форма содержит внешнюю ссылку"
_FORM_TEST_ASSIGNMENT_WARNING = "Форма содержит тестовое или испытательное задание"
_FORM_SOFTWARE_INSTALL_WARNING = "Форма предлагает установить программу"
_DANGEROUS_FORM_WARNINGS = frozenset(
    {
        _FORM_ATTACHMENT_WARNING,
        _FORM_EXTERNAL_LINK_WARNING,
        _FORM_TEST_ASSIGNMENT_WARNING,
        _FORM_SOFTWARE_INSTALL_WARNING,
    }
)

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
() => {
const publicationDate = (card, href) => {
    let vacancyId = '';
    try {
        vacancyId = new URL(href, window.location.href).pathname
            .match(/\\/vacancy\\/(\\d+)/)?.[1] || '';
    } catch {
        return '';
    }
    if (!vacancyId) return '';

    const fiberKey = Object.keys(card).find((key) => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? card[fiberKey] : null;
    for (let level = 0; fiber && level < 30; level += 1, fiber = fiber.return) {
        const props = fiber.memoizedProps;
        const candidates = [props, props?.vacancy];
        for (const candidate of candidates) {
            if (
                !candidate ||
                typeof candidate !== 'object' ||
                String(candidate.vacancyId || '') !== vacancyId
            ) {
                continue;
            }
            const publicationTime = candidate.publicationTime;
            if (typeof publicationTime === 'string' && publicationTime.trim()) {
                return publicationTime.trim();
            }
            if (!publicationTime || typeof publicationTime !== 'object') continue;
            if (
                typeof publicationTime.$ === 'string' &&
                publicationTime.$.trim()
            ) {
                return publicationTime.$.trim();
            }
            const timestamp = publicationTime['@timestamp'];
            if (typeof timestamp === 'number' && Number.isFinite(timestamp)) {
                return new Date(timestamp * 1000).toISOString();
            }
        }
    }
    return '';
};
return ({
    header: (
        document.querySelector('[data-qa="vacancies-search-header"]')?.textContent || ''
    ).trim(),
    vacancies: Array.from(
        document.querySelectorAll('[data-qa="vacancy-serp__vacancy"]')
    ).map((card) => {
        const titleLink = card.querySelector('[data-qa="serp-item__title"]');
        const href = titleLink?.href || '';
        return ({
            title: (titleLink?.textContent || '').trim(),
            href,
            employer: (
                card.querySelector(
                    '[data-qa="vacancy-serp__vacancy-employer"]'
                )?.textContent || ''
            ).trim(),
            region: (
                card.querySelector('[data-qa="vacancy-serp__vacancy-address"]')
                    ?.textContent || ''
            ).trim(),
            salary: (
                card.querySelector(
                    '[data-qa="vacancy-serp__vacancy-compensation"]'
                )?.textContent || ''
            ).trim(),
            publishedAt: (
                card.querySelector('time[datetime]')?.getAttribute('datetime') ||
                card.querySelector('[data-qa="vacancy-serp__vacancy-published-text"]')
                    ?.textContent ||
                publicationDate(card, href)
            ).trim(),
        });
    }),
})}
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
const externalApplicationText = /(?:forms\\.gle|docs\\.google\\.com\\/forms)/iu.test(bodyText);
const structuredPublicationDate = (() => {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            const pending = [JSON.parse(script.textContent || 'null')];
            for (let position = 0; position < pending.length && position < 2000; position += 1) {
                const candidate = pending[position];
                if (Array.isArray(candidate)) {
                    pending.push(...candidate);
                    continue;
                }
                if (!candidate || typeof candidate !== 'object') continue;
                const rawType = candidate['@type'];
                const types = Array.isArray(rawType) ? rawType : [rawType];
                if (types.some((value) => (
                    typeof value === 'string' && value.toLocaleLowerCase('en-US') === 'jobposting'
                ))) {
                    const value = candidate.datePosted || candidate.datePublished;
                    if (typeof value === 'string' && value.trim()) return value.trim();
                }
                pending.push(...Object.values(candidate).filter((value) => (
                    value && typeof value === 'object'
                )));
            }
        } catch {
            continue;
        }
    }
    return '';
})();
const visiblePublicationDate = bodyText
    .split('\\n')
    .map((line) => line.trim())
    .find((line) => /^Вакансия опубликована(?:\\s|$)/iu.test(line)) || '';
let availability = 'ACTIVE';
if (normalizedBody.includes('вакансия в архиве')) availability = 'ARCHIVED';
else if (normalizedBody.includes('вакансия закрыта')) availability = 'CLOSED';
else if (normalizedBody.includes('вакансия недоступна')) availability = 'UNAVAILABLE';
else if (normalizedBody.includes('недоступна эта вакансия')) availability = 'UNAVAILABLE';
else if (normalizedBody.includes('вакансия не найдена')) availability = 'UNAVAILABLE';
else if (normalizedBody.includes('такой вакансии нет')) availability = 'UNAVAILABLE';
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
        document.querySelector('[data-qa="vacancy-view-creation-time"] time[datetime]')
            ?.getAttribute('datetime') ||
        document.querySelector('[data-qa="vacancy-creation-time"] time[datetime]')
            ?.getAttribute('datetime') ||
        structuredPublicationDate ||
        document.querySelector('[data-qa="vacancy-view-creation-time"]')?.textContent ||
        document.querySelector('[data-qa="vacancy-creation-time"]')?.textContent ||
        visiblePublicationDate ||
        document.querySelector('time[datetime]')?.getAttribute('datetime') || ''
    ).trim(),
    hasCoverLetter: normalizedBody.includes('сопроводительн') && normalizedBody.includes('письм'),
    hasScreeningForm: normalizedBody.includes('вопросы работодателя') ||
        Boolean(document.querySelector('[data-qa="task-question"]')),
    hasExternalLink: externalLinks.length > 0 || externalApplicationText,
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
const vacancyIdFrom = (value) => {
    if (!value) return '';
    try {
        const url = new URL(value, window.location.href);
        const queryId = url.searchParams.get('vacancyId') || '';
        if (/^\\d+$/.test(queryId)) return queryId;
        return url.pathname.match(/^\\/vacancy\\/(\\d+)(?:\\/|$)/)?.[1] || '';
    } catch {
        return '';
    }
};
const questionNodes = Array.from(document.querySelectorAll('[data-qa="task-question"]'));
const fieldFromNode = (node, position) => {
    const fieldRoot = node.closest('[data-qa="task-body"]') || node;
    const control = fieldRoot.querySelector(
        'textarea, select, input:not([type="hidden"])'
    ) || fieldRoot.querySelector('[role="combobox"]');
    const question = clean(
        node.querySelector('label, legend, [data-qa*="question-title"]')?.textContent ||
        node.innerText
    );
    const qa = clean(control?.getAttribute('data-qa'));
    const name = clean(control?.getAttribute('name'));
    const id = clean(control?.getAttribute('id'));
    const controlIsInsideQuestion = Boolean(control && node.contains(control));
    const key = (
        controlIsInsideQuestion && qa ? `${position}:qa:${qa}` :
        controlIsInsideQuestion && name ? `${position}:name:${name}` :
        controlIsInsideQuestion && id ? `${position}:id:${id}` :
        `question:${position}:${question.toLocaleLowerCase('ru-RU')}`
    ).slice(0, 255);
    const tag = (control?.tagName || '').toLocaleLowerCase('en-US');
    const inputType = clean(control?.getAttribute('type')).toLocaleLowerCase('en-US');
    let fieldType = tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : inputType;
    if (!fieldType && control?.getAttribute('role') === 'combobox') fieldType = 'combobox';
    if (!fieldType) fieldType = control ? 'text' : 'unknown';
    const radioControls = Array.from(fieldRoot.querySelectorAll('input[type="radio"]'));
    const checkboxControls = Array.from(fieldRoot.querySelectorAll('input[type="checkbox"]'));
    const optionControls = radioControls.length
        ? radioControls
        : checkboxControls.length > 1 ? checkboxControls : [];
    const optionText = (option) => clean(
        option.closest('label, [data-qa="cell"]')?.innerText || option.value
    );
    const options = tag === 'select'
        ? Array.from(control.options || []).map(
            (option) => clean(option.textContent || option.value)
        )
            .filter(Boolean)
        : optionControls.map(optionText).filter(Boolean);
    if (radioControls.length) {
        fieldType = 'radio';
    } else if (checkboxControls.length > 1) {
        const normalizedOptions = new Set(
            options.map((option) => option.toLocaleLowerCase('ru-RU'))
        );
        fieldType = normalizedOptions.has('да') && normalizedOptions.has('нет')
            ? 'radio'
            : 'checkbox_group';
    }
    const maxLengthValue = Number.parseInt(control?.getAttribute('maxlength') || '', 10);
    const normalized = question.toLocaleLowerCase('ru-RU');
    const explicitlyOptional = (
        control?.getAttribute('aria-required') === 'false' ||
        normalized.includes('необязательно') ||
        normalized.includes('по желанию')
    );
    return {
        key,
        question,
        fieldType,
        isRequired: !explicitlyOptional,
        options,
        maxLength: Number.isFinite(maxLengthValue) && maxLengthValue > 0 ? maxLengthValue : null,
        formatHint: clean(
            control?.getAttribute('placeholder') || control?.getAttribute('inputmode')
        ),
        controlOutsideQuestion: Boolean(control && !controlIsInsideQuestion),
        hasAttachment: Boolean(fieldRoot.querySelector('input[type="file"]')),
        hasExternalAction: Boolean(fieldRoot.querySelector('a[href]')),
        hasTestAssignment: (
            normalized.includes('тестов') ||
            normalized.includes('испытательн') ||
            normalized.includes('домашн') ||
            normalized.includes('кодов') ||
            /(?:пройд\\S*|выполн\\S*)\\s+(?:тест\\S*|задан\\S*)/iu.test(normalized)
        ),
    };
};
const submit = document.querySelector('[data-qa="vacancy-response-submit-popup"]');
const responseForm = submit?.form || submit?.closest('form');
const responseDialog = submit?.closest('[role="dialog"]');
const responseContainer = submit?.closest('[data-qa*="vacancy-response"]');
const responseScopes = Array.from(new Set(
    [responseForm, responseDialog, responseContainer, submit?.parentElement].filter(Boolean)
));
const formText = clean(
    responseScopes.map((scope) => scope.innerText || '').join('\\n')
).toLocaleLowerCase('ru-RU');
const formWarnings = [];
if (responseScopes.some((scope) => scope.querySelector('input[type="file"]'))) {
    formWarnings.push('Форма содержит загрузку файла');
}
const responseLinks = Array.from(new Set(
    responseScopes.flatMap((scope) => Array.from(scope.querySelectorAll('a[href]')))
));
const hasExternalLink = responseLinks.some((link) => {
    try {
        const target = new URL(link.href, window.location.href);
        const host = target.hostname.toLocaleLowerCase('en-US');
        if (target.protocol !== 'http:' && target.protocol !== 'https:') return true;
        return host !== 'hh.ru' && !host.endsWith('.hh.ru');
    } catch {
        return true;
    }
});
if (hasExternalLink) formWarnings.push('Форма содержит внешнюю ссылку');
if (
    /(?:(?:тестов\\S*|испытательн\\S*|домашн\\S*|кодов\\S*)\\s+задан|(?:пройд\\S*|выполн\\S*)\\s+(?:тест\\S*|задан\\S*)|coding\\s+challenge|test\\s+assignment)/iu.test(
        formText
    )
) {
    formWarnings.push('Форма содержит тестовое или испытательное задание');
}
if (
    /(?:установ\\S*|инсталлир\\S*)\\s+(?:программ\\S*|приложен\\S*)|install\\s+(?:an?\\s+)?(?:app|software|program)/iu.test(
        formText
    )
) {
    formWarnings.push('Форма предлагает установить программу');
}
const resumeTitleNode = document.querySelector('[data-qa="resume-title"]');
const resumeIdCandidates = [];
const addResumeId = (value) => {
    const candidate = clean(value);
    if (candidate && candidate.length <= 255 && !/\\s/u.test(candidate)) {
        resumeIdCandidates.push(candidate);
    }
};
const addResumeIdFromUrl = (value) => {
    if (!value) return;
    try {
        const target = new URL(value, window.location.href);
        addResumeId(
            target.searchParams.get('resumeId') ||
            target.searchParams.get('resume_id') ||
            target.searchParams.get('resumeHash') ||
            target.searchParams.get('resume_hash')
        );
    } catch {
        // Не используем неразбираемый адрес как подтверждение выбранного резюме.
    }
};
for (const input of document.querySelectorAll(
    'input[name="resumeId"]:checked, input[name="resume_id"]:checked, ' +
    'input[name="resumeHash"]:checked, input[name="resume_hash"]:checked'
)) {
    addResumeId(input.value);
}
for (const input of responseForm?.querySelectorAll(
    'input[type="hidden"][name="resumeId"], input[type="hidden"][name="resume_id"], ' +
    'input[type="hidden"][name="resumeHash"], input[type="hidden"][name="resume_hash"]'
) || []) {
    addResumeId(input.value);
}
addResumeIdFromUrl(submit?.getAttribute('formaction'));
addResumeIdFromUrl(responseForm?.getAttribute('action'));
for (const node of [
    resumeTitleNode,
    resumeTitleNode?.closest('[data-resume-id], [data-resume-hash]'),
]) {
    addResumeId(node?.getAttribute('data-resume-id'));
    addResumeId(node?.getAttribute('data-resume-hash'));
}
const selectedOption = document.querySelector(
    '[role="option"][aria-selected="true"][data-qa^="magritte-select-option-"]'
);
const selectedOptionQa = clean(selectedOption?.getAttribute('data-qa'));
if (selectedOptionQa.startsWith('magritte-select-option-')) {
    addResumeId(selectedOptionQa.slice('magritte-select-option-'.length));
}
const resumeLink = resumeTitleNode?.closest('a[href*="/resume/"]') ||
    resumeTitleNode?.querySelector('a[href*="/resume/"]');
try {
    const resumePathId = new URL(resumeLink?.href || '', window.location.href)
        .pathname.match(/^\\/resume\\/([^/?#]+)(?:\\/|$)/u)?.[1] || '';
    addResumeId(resumePathId);
} catch {
    // Не используем неразбираемую ссылку как подтверждение выбранного резюме.
}
const selectedResumeHashesFromReact = (node) => {
    const resumeHash = (value) => {
        if (value === null || typeof value !== 'object') return '';
        const attributes = value._attributes;
        if (attributes === null || typeof attributes !== 'object') return '';
        const hash = clean(value.hash || attributes.hash);
        const looksLikeResume = (
            (
                'shortExperience' in value ||
                'specialization' in value ||
                'primaryEducation' in value
            ) &&
            ('hhid' in attributes || 'id' in attributes)
        );
        return looksLikeResume && /^[a-f0-9]{32,64}$/iu.test(hash) ? hash : '';
    };
    const hashesFrom = (...values) => {
        const hashes = new Set();
        const visited = new WeakSet();
        let visitedCount = 0;
        const scan = (value, depth) => {
            if (
                value === null || typeof value !== 'object' ||
                depth > 8 || visitedCount > 4000 || visited.has(value)
            ) {
                return;
            }
            visited.add(value);
            visitedCount += 1;
            const directResumeHash = resumeHash(value);
            if (directResumeHash) hashes.add(directResumeHash);
            for (const [key, nested] of Object.entries(value)) {
                const normalizedKey = key.replace(/[_-]/g, '').toLocaleLowerCase('en-US');
                if (
                    typeof nested === 'string' &&
                    (
                        normalizedKey === 'resumehash' ||
                        normalizedKey === 'selectedresumehash'
                    ) &&
                    /^[a-f0-9]{32,64}$/iu.test(nested)
                ) {
                    hashes.add(nested);
                    continue;
                }
                scan(nested, depth + 1);
            }
        };
        for (const value of values) scan(value, 0);
        return hashes;
    };
    let current = node;
    for (let level = 0; current && level < 6; level += 1, current = current.parentElement) {
        for (const key of Object.keys(current)) {
            if (key.startsWith('__reactFiber$')) {
                let fiber = current[key];
                for (let fiberLevel = 0; fiber && fiberLevel < 12; fiberLevel += 1) {
                    const hashes = hashesFrom(fiber.pendingProps, fiber.memoizedProps);
                    if (hashes.size === 1) return Array.from(hashes);
                    fiber = fiber.return;
                }
            } else if (key.startsWith('__reactProps$')) {
                const hashes = hashesFrom(current[key]);
                if (hashes.size === 1) return Array.from(hashes);
            }
        }
    }
    return [];
};
for (const resumeHash of selectedResumeHashesFromReact(resumeTitleNode)) {
    addResumeId(resumeHash);
}
const uniqueResumeIds = Array.from(new Set(resumeIdCandidates));
const hiddenVacancyId = responseForm?.querySelector(
    'input[name="vacancyId"], input[name="vacancy_id"]'
)?.value || '';
const formVacancyIds = [
    vacancyIdFrom(submit?.getAttribute('formaction')),
    vacancyIdFrom(responseForm?.getAttribute('action')),
    /^\\d+$/.test(hiddenVacancyId) ? hiddenVacancyId : '',
].filter(Boolean);
const uniqueFormVacancyIds = Array.from(new Set(formVacancyIds));
const pageVacancyId = vacancyIdFrom(window.location.href);
return ({
    fields: questionNodes.map(fieldFromNode).filter((field) => field.question),
    warnings: Array.from(new Set([
        ...Array.from(
            document.querySelectorAll('[data-qa="response-reject-warning"]')
        ).map((node) => (node.innerText || '').trim().replace(/\\s+/g, ' ')),
        ...formWarnings,
    ])).filter(Boolean),
    resumeTitle: (resumeTitleNode?.textContent || '').trim(),
    resumeHhId: uniqueResumeIds.length === 1 ? uniqueResumeIds[0] : '',
    coverLetter: (
        document.querySelector(
            '[data-qa="vacancy-response-popup-form-letter-input"]'
        )?.value || ''
    ),
    bodyText: (document.body.innerText || '').trim(),
    vacancyId: uniqueFormVacancyIds.length === 1
        ? uniqueFormVacancyIds[0]
        : uniqueFormVacancyIds.length === 0 ? pageVacancyId : '',
});
}
"""

FILL_APPLICATION_FORM_SCRIPT = """
(answers) => {
const clean = (value) => (value || '').trim().replace(/\\s+/g, ' ');
const normalized = (value) => clean(value).toLocaleLowerCase('ru-RU');
const nodes = Array.from(document.querySelectorAll('[data-qa="task-question"]'));
const controls = nodes.map((node, position) => {
    const fieldRoot = node.closest('[data-qa="task-body"]') || node;
    const control = fieldRoot.querySelector(
        'textarea, select, input:not([type="hidden"])'
    ) || fieldRoot.querySelector('[role="combobox"]');
    const question = clean(
        node.querySelector('label, legend, [data-qa*="question-title"]')?.textContent ||
        node.innerText
    );
    const qa = clean(control?.getAttribute('data-qa'));
    const name = clean(control?.getAttribute('name'));
    const id = clean(control?.getAttribute('id'));
    const controlIsInsideQuestion = Boolean(control && node.contains(control));
    const key = (
        controlIsInsideQuestion && qa ? `${position}:qa:${qa}` :
        controlIsInsideQuestion && name ? `${position}:name:${name}` :
        controlIsInsideQuestion && id ? `${position}:id:${id}` :
        `question:${position}:${question.toLocaleLowerCase('ru-RU')}`
    ).slice(0, 255);
    return {key, node: fieldRoot, control};
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
        const checkboxes = Array.from(item.node.querySelectorAll('input[type="checkbox"]'));
        if (checkboxes.length > 1) {
            const checkbox = checkboxes.find(
                (candidate) => normalized(candidate.value) === normalized(value) ||
                    normalized(
                        candidate.closest('label, [data-qa="cell"]')?.innerText
                    ) === normalized(value)
            );
            if (!checkbox) { skipped.push(answer.key); continue; }
            for (const candidate of checkboxes) {
                if (candidate !== checkbox && candidate.checked) candidate.click();
            }
            if (!checkbox.checked) checkbox.click();
        } else {
            const shouldCheck = ['да', 'true', '1', 'согласен'].includes(normalized(value));
            if (control.checked !== shouldCheck) control.click();
        }
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

VERIFY_APPLICATION_FORM_SCRIPT = """
(answers) => {
const clean = (value) => (value || '').trim().replace(/\\s+/g, ' ');
const normalized = (value) => clean(value).toLocaleLowerCase('ru-RU');
const expected = new Map(answers.map((answer) => [answer.key, clean(answer.value)]));
const nodes = Array.from(document.querySelectorAll('[data-qa="task-question"]'));
const controls = nodes.map((node, position) => {
    const fieldRoot = node.closest('[data-qa="task-body"]') || node;
    const control = fieldRoot.querySelector(
        'textarea, select, input:not([type="hidden"])'
    ) || fieldRoot.querySelector('[role="combobox"]');
    const question = clean(
        node.querySelector('label, legend, [data-qa*="question-title"]')?.textContent ||
        node.innerText
    );
    const qa = clean(control?.getAttribute('data-qa'));
    const name = clean(control?.getAttribute('name'));
    const id = clean(control?.getAttribute('id'));
    const controlIsInsideQuestion = Boolean(control && node.contains(control));
    const key = (
        controlIsInsideQuestion && qa ? `${position}:qa:${qa}` :
        controlIsInsideQuestion && name ? `${position}:name:${name}` :
        controlIsInsideQuestion && id ? `${position}:id:${id}` :
        `question:${position}:${question.toLocaleLowerCase('ru-RU')}`
    ).slice(0, 255);
    const questionText = question.toLocaleLowerCase('ru-RU');
    const required = !(
        control?.getAttribute('aria-required') === 'false' ||
        questionText.includes('необязательно') ||
        questionText.includes('по желанию')
    );
    return {key, node: fieldRoot, control, required};
});
const missingRequired = [];
const mismatched = [];
for (const item of controls) {
    const value = expected.get(item.key);
    const control = item.control;
    if (!control) {
        if (item.required) missingRequired.push(item.key);
        continue;
    }
    const tag = control.tagName.toLocaleLowerCase('en-US');
    const type = clean(control.getAttribute('type')).toLocaleLowerCase('en-US');
    let actual = '';
    let present = false;
    if (tag === 'select') {
        const selected = control.selectedOptions?.[0];
        actual = clean(selected?.textContent || selected?.value || control.value);
        present = Boolean(clean(control.value) || actual);
    } else if (type === 'radio') {
        const selected = item.node.querySelector('input[type="radio"]:checked');
        actual = clean(selected?.closest('label')?.innerText || selected?.value);
        present = Boolean(selected);
    } else if (type === 'checkbox') {
        const checkboxes = Array.from(item.node.querySelectorAll('input[type="checkbox"]'));
        if (checkboxes.length > 1) {
            const selected = checkboxes.filter((candidate) => candidate.checked);
            actual = selected.map((candidate) => clean(
                candidate.closest('label, [data-qa="cell"]')?.innerText || candidate.value
            )).join(', ');
            present = selected.length > 0;
        } else {
            actual = control.checked ? 'да' : 'нет';
            present = !item.required || control.checked;
        }
    } else {
        actual = clean(control.value);
        present = Boolean(actual);
    }
    if (item.required && !present) missingRequired.push(item.key);
    if (value !== undefined && normalized(actual) !== normalized(value)) {
        mismatched.push(item.key);
    }
}
return {missingRequired, mismatched};
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
    const vacancyIdFor = (candidate) => {
        const link = candidate.querySelector('a[href*="/vacancy/"]');
        if (link) {
            const direct = new URL(link.href, location.href).pathname
                .match(/^\\/vacancy\\/(\\d+)(?:\\/|$)/)?.[1] || '';
            if (direct) return direct;
        }
        const fiberKey = Object.getOwnPropertyNames(candidate).find(
            (key) => key.startsWith('__reactFiber$')
        );
        let fiber = fiberKey ? candidate[fiberKey] : null;
        for (let level = 0; fiber && level < 12; level += 1) {
            const value = (
                fiber.memoizedProps?.topic?.vacancyId ||
                fiber.pendingProps?.topic?.vacancyId
            );
            if (typeof value === 'number' || typeof value === 'string') {
                return String(value);
            }
            fiber = fiber.return;
        }
        return '';
    };
    const item = Array.from(
        document.querySelectorAll('[data-qa="negotiations-item"]')
    ).find((candidate) => vacancyIdFor(candidate) === String(vacancyId));
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
    resume_hh_id: str
    cover_letter: str
    body_text: str
    vacancy_id: str
    compatible_version_hashes: tuple[str, ...] = ()

    @property
    def questions(self) -> tuple[str, ...]:
        return tuple(field.question for field in self.screening_form.fields)

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.screening_form.warnings


@dataclass(slots=True)
class _SubmissionAttempt:
    started: bool = False
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class _OutgoingMessagesSnapshot:
    exact_count: int
    message_ids: frozenset[str]


class _ResumeSelectionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class _BrowserProfileLock:
    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = _PROFILE_LOCK_TIMEOUT_SECONDS,
        retry_seconds: float = _PROFILE_LOCK_RETRY_SECONDS,
    ) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._retry_seconds = retry_seconds
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("Профиль браузера уже занят этим процессом")

        handle = self._path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        deadline = monotonic() + self._timeout_seconds
        while True:
            try:
                self._try_lock(handle)
            except OSError as error:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    handle.close()
                    raise RuntimeError(
                        "Профиль hh.ru занят другой задачей дольше допустимого времени"
                    ) from error
                sleep(min(self._retry_seconds, remaining))
            else:
                self._handle = handle
                return

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            with suppress(OSError):
                self._unlock(handle)
        finally:
            handle.close()

    @staticmethod
    def _try_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _HhHttpProxy:
    def __init__(self, source_host: str) -> None:
        self._source_host = source_host
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._resolved_hosts: dict[str, str] = {}
        self._resolved_hosts_lock = threading.Lock()

    @property
    def pac_url(self) -> str:
        if self._port is None:
            raise RuntimeError("Локальный канал hh.ru ещё не запущен")
        return f"http://{_HH_PROXY_HOST}:{self._port}/proxy.pac"

    def start(self) -> None:
        if self._thread is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((_HH_PROXY_HOST, 0))
            listener.listen()
            listener.settimeout(0.5)
        except OSError as error:
            listener.close()
            raise RuntimeError("Не удалось открыть локальный канал к hh.ru") from error
        self._stop.clear()
        self._listener = listener
        self._port = listener.getsockname()[1]
        self._thread = threading.Thread(
            target=self._serve,
            name="hugin-hh-network-proxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        self._port = None
        if listener is not None:
            with suppress(OSError):
                listener.close()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2)

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                client, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._relay,
                args=(client,),
                name="hugin-hh-network-connection",
                daemon=True,
            ).start()

    def _relay(self, client: socket.socket) -> None:
        remote: socket.socket | None = None
        self._track(client)
        try:
            client.settimeout(5)
            request = self._request_head(client)
            first_line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            if first_line.startswith("GET /proxy.pac "):
                self._serve_pac(client)
                return
            parts = first_line.split()
            target_host, separator, target_port = (
                parts[1].casefold().rpartition(":") if len(parts) == 3 else ("", "", "")
            )
            if (
                len(parts) != 3
                or parts[0].upper() != "CONNECT"
                or not separator
                or target_port != str(_HH_HTTPS_PORT)
                or not self._allowed_host(target_host)
            ):
                client.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
                return
            remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._track(remote)
            remote.settimeout(15)
            remote.bind((self._source_host, 0))
            remote.connect((self._resolve_host(target_host), _HH_HTTPS_PORT))
            remote.settimeout(None)
            client.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\nConnection: keep-alive\r\n\r\n")
            upstream = threading.Thread(
                target=self._copy,
                args=(client, remote),
                name="hugin-hh-network-upload",
                daemon=True,
            )
            upstream.start()
            self._copy(remote, client)
            upstream.join(timeout=1)
        except OSError:
            return
        finally:
            connections = (client,) if remote is None else (client, remote)
            self._untrack(*connections)
            for connection in connections:
                with suppress(OSError):
                    connection.close()

    @staticmethod
    def _allowed_host(host: str) -> bool:
        return host in {"hh.ru", "hhcdn.ru"} or host.endswith(_HH_PROXY_ALLOWED_SUFFIXES)

    def _resolve_host(self, host: str) -> str:
        with self._resolved_hosts_lock:
            if cached := self._resolved_hosts.get(host):
                return cached
        resolved: str | None = None
        request = urllib.request.Request(
            f"{_HH_DOH_URL}?{urlencode({'name': host, 'type': 'A'})}",
            headers={"Accept": "application/dns-json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            answers = payload.get("Answer") if isinstance(payload, dict) else None
            for answer in answers if isinstance(answers, list) else ():
                if not isinstance(answer, dict) or answer.get("type") != 1:
                    continue
                value = answer.get("data")
                if not isinstance(value, str):
                    continue
                try:
                    resolved = str(IPv4Address(value))
                except ValueError:
                    continue
                break
        if resolved is None:
            try:
                addresses = socket.getaddrinfo(
                    host,
                    _HH_HTTPS_PORT,
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                )
            except socket.gaierror:
                addresses = []
            resolved = next(
                (
                    address[4][0]
                    for address in addresses
                    if isinstance(address[4], tuple)
                    and address[4]
                    and isinstance(address[4][0], str)
                ),
                None,
            )
        if resolved is None:
            raise OSError(f"Не удалось определить адрес {host}")
        with self._resolved_hosts_lock:
            self._resolved_hosts[host] = resolved
        return resolved

    @staticmethod
    def _request_head(client: socket.socket) -> bytes:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = client.recv(4_096)
            if not chunk:
                break
            request.extend(chunk)
            if len(request) > 16_384:
                raise OSError("Слишком большой запрос к локальному каналу")
        return bytes(request)

    def _serve_pac(self, client: socket.socket) -> None:
        if self._port is None:
            raise OSError("Локальный канал остановлен")
        body = (
            "function FindProxyForURL(url, host) {\n"
            '  if (host === "hh.ru" || dnsDomainIs(host, ".hh.ru") ||\n'
            '      host === "hhcdn.ru" || dnsDomainIs(host, ".hhcdn.ru"))\n'
            f'    return "PROXY {_HH_PROXY_HOST}:{self._port}";\n'
            '  return "DIRECT";\n'
            "}\n"
        ).encode()
        client.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/x-ns-proxy-autoconfig\r\n"
            b"Cache-Control: no-store\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )

    @staticmethod
    def _copy(source: socket.socket, destination: socket.socket) -> None:
        try:
            while data := source.recv(65_536):
                destination.sendall(data)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                destination.shutdown(socket.SHUT_WR)

    def _track(self, *connections: socket.socket) -> None:
        with self._connections_lock:
            self._connections.update(connections)

    def _untrack(self, *connections: socket.socket) -> None:
        with self._connections_lock:
            self._connections.difference_update(connections)


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
        browser_source_ip: str | None = None,
        profile_lock_timeout_seconds: float | None = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._login_url = login_url
        self._resumes_url = resumes_url
        self._search_url = search_url
        self._timeout_ms = timeout_ms
        self._start_minimized = start_minimized
        self._browser_source_ip = browser_source_ip
        self._network_proxy: _HhHttpProxy | None = None
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        lock_timeout = (
            _PROFILE_LOCK_TIMEOUT_SECONDS
            if profile_lock_timeout_seconds is None
            else profile_lock_timeout_seconds
        )
        if lock_timeout < 0:
            raise ValueError("Время ожидания профиля браузера не может быть отрицательным")
        self._profile_lock = _BrowserProfileLock(
            self._profile_dir / _PROFILE_LOCK_FILENAME,
            timeout_seconds=lock_timeout,
        )

    def __enter__(self) -> VisibleHhBrowser:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_lock.acquire()
        try:
            self._playwright = sync_playwright().start()
            chromium_args = (
                [
                    "--start-minimized",
                    "--mute-audio",
                ]
                if self._start_minimized
                else ["--start-maximized"]
            )
            source_ip = usable_source_ipv4(self._browser_source_ip)
            if source_ip:
                self._network_proxy = _HhHttpProxy(source_ip)
                self._network_proxy.start()
                chromium_args.append(f"--proxy-pac-url={self._network_proxy.pac_url}")
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
        except BaseException:
            if self._playwright is not None:
                with suppress(Exception):
                    self._playwright.stop()
            self._playwright = None
            self._context = None
            self._page = None
            if self._network_proxy is not None:
                self._network_proxy.stop()
                self._network_proxy = None
            self._profile_lock.release()
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
        try:
            if self._context is not None:
                self._context.close()
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._context = None
                self._page = None
                self._playwright = None
                if self._network_proxy is not None:
                    self._network_proxy.stop()
                    self._network_proxy = None
                self._profile_lock.release()

    def open_login(self) -> None:
        page = self._require_page()
        try:
            response = page.goto(self._login_url, wait_until="commit")
            if response is not None and response.status == 429:
                raise HhSyncRetryableError(
                    "HH_RATE_LIMITED",
                    "hh.ru временно ограничил обращения при входе",
                    retry_after_seconds=(
                        self._retry_after_seconds(response) or _TEMPORARY_REQUEST_RETRY_SECONDS
                    ),
                )
            if response is not None and response.status == 403:
                raise HhSyncBlockedError(
                    "ACCOUNT_WARNING",
                    "hh.ru запретил вход и требует ручной проверки аккаунта",
                )
            page.wait_for_load_state(
                "domcontentloaded",
                timeout=self._timeout_ms,
            )
            self._wait_for_login_surface(page)
        except PlaywrightError as error:
            message = str(error)
            if "ERR_ABORTED" in message:
                page.wait_for_timeout(500)
                if self.is_authenticated():
                    return
            if isinstance(error, PlaywrightTimeoutError) or any(
                marker in message for marker in _TEMPORARY_NAVIGATION_ERROR_MARKERS
            ):
                raise HhSyncRetryableError(
                    "HH_NETWORK_TIMEOUT",
                    "Страница входа hh.ru временно недоступна; "
                    "проверка будет повторена автоматически",
                    retry_after_seconds=_NETWORK_RETRY_SECONDS,
                ) from error
            raise

    def is_open(self) -> bool:
        page = self._page
        if page is None or page.is_closed():
            return False
        try:
            page.evaluate(_WINDOW_LIVENESS_SCRIPT)
        except PlaywrightError:
            return False
        return True

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
            response = page.goto(normalized_url, wait_until="domcontentloaded")
            payload = page.evaluate(VACANCY_DETAILS_SCRIPT)
            availability = self._vacancy_availability(response, payload)
            if availability is VacancyAvailability.ACTIVE and self._vacancy_is_closed(
                response,
                self._page_body_text(page),
            ):
                availability = VacancyAvailability.UNAVAILABLE
            if availability is not VacancyAvailability.ACTIVE:
                raise VacancyUnavailableError(vacancy_id, normalized_url, availability)
            page.locator('[data-qa="vacancy-title"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
            payload = page.evaluate(VACANCY_DETAILS_SCRIPT)
            availability = self._vacancy_availability(response, payload)
            if availability is VacancyAvailability.ACTIVE and self._vacancy_is_closed(
                response,
                self._page_body_text(page),
            ):
                availability = VacancyAvailability.UNAVAILABLE
            if availability is not VacancyAvailability.ACTIVE:
                raise VacancyUnavailableError(vacancy_id, normalized_url, availability)
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"Страница вакансии {vacancy_id} не загрузилась") from error
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
        payload = self._negotiations_payload(page)
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
        page = self._open_negotiations()
        messages: list[HhChatMessageData] = []
        read_ids: set[str] = set()
        page_numbers = self._negotiation_page_numbers(page)
        pages: tuple[int | None, ...] = page_numbers or (None,)
        for page_number in pages:
            if page_number is not None:
                self._select_negotiation_page(page, page_number)
            for item in self._negotiations_payload(page):
                if item.get("chatAvailable") is not True:
                    continue
                vacancy_id = self._optional_string(item, "vacancyId")
                if vacancy_id and not vacancy_id.isdigit():
                    raise RuntimeError("hh.ru вернул некорректный номер вакансии с перепиской")
                vacancy_id = vacancy_id or self._vacancy_id_from_href(
                    self._optional_string(item, "vacancyHref")
                )
                if not vacancy_id:
                    raise RuntimeError("hh.ru не вернул номер вакансии для доступной переписки")
                if vacancy_id not in selected_ids or vacancy_id in read_ids:
                    continue
                self._read_recruiter_chat(page, vacancy_id, messages)
                read_ids.add(vacancy_id)
        return tuple(messages)

    def _read_recruiter_chat(
        self,
        page: Page,
        vacancy_id: str,
        messages: list[HhChatMessageData],
    ) -> None:
        opened = page.evaluate(OPEN_NEGOTIATION_CHAT_SCRIPT, vacancy_id)
        if opened is not True:
            raise RuntimeError(
                f"hh.ru показал переписку вакансии {vacancy_id}, но не открыл её для чтения"
            )
        frame = self._wait_for_chat_frame(page)
        if frame is None:
            raise RuntimeError(f"Переписка вакансии {vacancy_id} не загрузилась")
        payload = self._read_chat_messages(page, frame, vacancy_id)
        for item in payload:
            message_vacancy_id = self._required_string(
                item,
                "vacancyId",
                "идентификатора вакансии сообщения",
            )
            if message_vacancy_id != vacancy_id:
                raise RuntimeError("hh.ru вернул сообщения из другой переписки")
            raw_direction = self._required_string(
                item,
                "direction",
                "направления сообщения",
            )
            try:
                direction = MessageDirection(raw_direction)
            except ValueError as error:
                raise RuntimeError("hh.ru вернул неизвестное направление сообщения") from error
            messages.append(
                HhChatMessageData(
                    vacancy_id=message_vacancy_id,
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
        if close.count() != 1:
            raise RuntimeError(f"Переписка вакансии {vacancy_id} не показала кнопку закрытия")
        try:
            close.first.click(no_wait_after=True)
        except PlaywrightError as error:
            raise RuntimeError(f"Не удалось закрыть переписку вакансии {vacancy_id}") from error
        page.wait_for_timeout(500)

    @staticmethod
    def _negotiations_payload(page: Page) -> list[dict[object, object]]:
        payload = page.evaluate(NEGOTIATIONS_SCRIPT)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise RuntimeError("hh.ru вернул некорректный список откликов")
        return payload

    def _read_chat_messages(
        self,
        page: Page,
        frame: Frame,
        vacancy_id: str,
    ) -> list[dict[object, object]]:
        attempts = max(min(self._timeout_ms // 500, 20), 1)
        for _attempt in range(attempts):
            try:
                payload = frame.evaluate(CHAT_MESSAGES_SCRIPT, vacancy_id)
            except PlaywrightError as error:
                raise RuntimeError(
                    f"Не удалось прочитать переписку вакансии {vacancy_id}"
                ) from error
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise RuntimeError("hh.ru вернул некорректную переписку")
            if payload:
                return payload
            page.wait_for_timeout(500)
        raise RuntimeError(
            f"Переписка вакансии {vacancy_id} открылась, но hh.ru не отдал ни одного сообщения"
        )

    def apply_to_vacancy(
        self,
        source_url: str,
        *,
        expected_resume_hh_id: str,
        expected_resume_title: str,
        cover_letter: str,
        submit: bool = False,
        submit_guard: Callable[[], bool] | None = None,
        screening_submission: HhScreeningSubmission | None = None,
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
                screening_submission=screening_submission,
                attempt=attempt,
            )
        except Exception as error:
            details = str(error).strip().splitlines()[0][:500]
            suffix = f": {details}" if details else ""
            if attempt.started:
                return HhApplyResult(
                    HhApplyStatus.UNKNOWN_RESULT,
                    page.url,
                    f"Ошибка после начала отправки: {type(error).__name__}{suffix}",
                )
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                f"Ошибка до нажатия кнопки отправки: {type(error).__name__}{suffix}",
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
        screening_submission: HhScreeningSubmission | None,
        attempt: _SubmissionAttempt,
    ) -> HhApplyResult:
        vacancy_id, _normalized_url = self._vacancy_id_and_url(source_url)
        vacancy_url = self._canonical_vacancy_url(source_url)
        page = self._require_page()
        try:
            initial_response = page.goto(vacancy_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1_500)
        except PlaywrightError as error:
            details = str(error).strip().splitlines()[0][:500]
            is_network_error = isinstance(error, PlaywrightTimeoutError) or any(
                marker in details for marker in _TEMPORARY_NAVIGATION_ERROR_MARKERS
            )
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                f"Не загрузилась страница вакансии: {details or type(error).__name__}",
                retry_after_seconds=(_NETWORK_RETRY_SECONDS if is_network_error else None),
            )
        body_text = self._page_body_text(page)
        if initial_response is not None and initial_response.status == 429:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "hh.ru временно ограничил обращения; отклик будет повторён автоматически",
                retry_after_seconds=(
                    self._retry_after_seconds(initial_response) or _TEMPORARY_REQUEST_RETRY_SECONDS
                ),
            )
        if self._vacancy_is_closed(initial_response, body_text):
            return HhApplyResult(HhApplyStatus.VACANCY_CLOSED, page.url)
        if initial_response is not None and initial_response.status == 403:
            return HhApplyResult(
                HhApplyStatus.ACCOUNT_WARNING,
                page.url,
                "hh.ru запретил доступ к странице; требуется ручная проверка аккаунта",
            )
        access_error = self._application_access_error(
            page,
            body_text,
        )
        if access_error is not None:
            return access_error
        vacancy_error = self._application_vacancy_error(page, vacancy_id)
        if vacancy_error is not None:
            return vacancy_error
        response_links = page.locator('[data-qa="vacancy-response-link-top"]:visible')
        if response_links.count() == 0:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "На странице вакансии нет стандартной кнопки отклика",
            )
        try:
            response_links.first.click(no_wait_after=True, timeout=min(self._timeout_ms, 10_000))
            page.locator('[data-qa="resume-title"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
            page.wait_for_timeout(500)
        except PlaywrightError as error:
            details = str(error).strip().splitlines()[0][:500]
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                f"Кнопка отклика не открыла форму: {details or type(error).__name__}",
            )

        initial = self._application_snapshot(page)
        vacancy_error = self._application_vacancy_error(page, vacancy_id, initial)
        if vacancy_error is not None:
            return vacancy_error
        body_text = initial.body_text
        access_error = self._application_access_error(page, body_text)
        if access_error is not None:
            return access_error
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhApplyResult(HhApplyStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhApplyResult(
                HhApplyStatus.ALREADY_APPLIED,
                page.url,
                body_text[:1000],
                screening_form_version_hash=(
                    screening_submission.version_hash if screening_submission is not None else None
                ),
            )
        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() == 1 and self._contains_any(
            submit_button.first.inner_text(),
            "повторно",
        ):
            return HhApplyResult(
                HhApplyStatus.ALREADY_APPLIED,
                page.url,
                body_text[:1000],
                screening_form_version_hash=(
                    screening_submission.version_hash if screening_submission is not None else None
                ),
            )

        try:
            initial = self._select_exact_resume(
                page,
                expected_resume_hh_id=expected_resume_hh_id,
                expected_resume_title=expected_resume_title,
                current_snapshot=initial,
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
        vacancy_error = self._application_vacancy_error(page, vacancy_id, initial)
        if vacancy_error is not None:
            return vacancy_error
        body_text = initial.body_text
        access_error = self._application_access_error(page, body_text)
        if access_error is not None:
            return access_error
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhApplyResult(HhApplyStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhApplyResult(
                HhApplyStatus.ALREADY_APPLIED,
                page.url,
                body_text[:1000],
                screening_form_version_hash=(
                    screening_submission.version_hash if screening_submission is not None else None
                ),
            )
        if initial.questions:
            if screening_submission is None:
                return self._questions_required(page.url, initial)
            if self._screening_form_is_dangerous(initial.screening_form):
                return self._dangerous_screening_form_result(page.url, initial)
            if screening_form_hash(initial.screening_form) != screening_submission.version_hash:
                return self._questions_required(
                    page.url,
                    initial,
                    "Состав анкеты изменился; ответы будут проверены заново",
                )
            fill_error = self._fill_and_verify_screening_form(
                page,
                screening_submission,
            )
            if fill_error is not None:
                return HhApplyResult(
                    HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                    page.url,
                    fill_error,
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
                    return HhApplyResult(
                        HhApplyStatus.RETRYABLE_ERROR,
                        page.url,
                        "В форме нет однозначной кнопки добавления сопроводительного письма",
                    )
                toggle.click()
            letter.first.wait_for(state="visible", timeout=self._timeout_ms)
            letter.first.fill(cover_letter.strip())

        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() != 1:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "В открытой форме нет единственной кнопки отправки",
            )
        if not submit:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Форма заполнена и оставлена открытой без отправки",
                warnings=initial.warnings,
            )
        if not submit_button.first.is_enabled():
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "Кнопка отправки недоступна после заполнения формы",
            )
        if submit_guard is None:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Форма заполнена и оставлена открытой без отправки",
                warnings=initial.warnings,
            )

        final = self._application_snapshot(page)
        vacancy_error = self._application_vacancy_error(page, vacancy_id, final)
        if vacancy_error is not None:
            return vacancy_error
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
            if screening_submission is None:
                return self._questions_required(page.url, final)
            if self._screening_form_is_dangerous(final.screening_form):
                return self._dangerous_screening_form_result(page.url, final)
            if screening_form_hash(final.screening_form) != screening_submission.version_hash:
                return self._questions_required(
                    page.url,
                    final,
                    "Состав анкеты изменился перед отправкой; ответы будут проверены заново",
                )
            fill_error = self._fill_and_verify_screening_form(
                page,
                screening_submission,
            )
            if fill_error is not None:
                return HhApplyResult(
                    HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                    page.url,
                    fill_error,
                    questions=final.questions,
                    warnings=final.warnings,
                    screening_form=final.screening_form,
                )
        elif screening_submission is not None:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Перед отправкой ранее проверенная анкета исчезла; кнопка не нажата",
            )

        ready = self._application_snapshot(page)
        access_error = self._application_access_error(page, ready.body_text)
        if access_error is not None:
            return access_error
        ready_error = self._ready_application_error(
            page,
            ready,
            vacancy_id=vacancy_id,
            expected_resume_hh_id=expected_resume_hh_id,
            expected_resume_title=expected_resume_title,
            expected_cover_letter=cover_letter.strip(),
            screening_submission=screening_submission,
        )
        if ready_error is not None:
            return ready_error
        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() != 1 or not submit_button.first.is_enabled():
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "Перед нажатием кнопка отправки исчезла или стала недоступна",
                warnings=ready.warnings,
            )
        try:
            submission_allowed = submit_guard()
        except Exception as error:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                (f"Не удалось повторно проверить данные перед отправкой: {type(error).__name__}"),
                warnings=ready.warnings,
            )
        if not submission_allowed:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Перед отправкой изменились проверенные данные; кнопка не нажата",
                warnings=ready.warnings,
            )

        parsed = urlparse(self._resumes_url)
        response: Response | None = None
        submit_button.first.click(
            trial=True,
            timeout=min(self._timeout_ms, 10_000),
        )
        click_ready = self._application_snapshot(page)
        click_access_error = self._application_access_error(page, click_ready.body_text)
        if click_access_error is not None:
            return click_access_error
        click_ready_error = self._ready_application_error(
            page,
            click_ready,
            vacancy_id=vacancy_id,
            expected_resume_hh_id=expected_resume_hh_id,
            expected_resume_title=expected_resume_title,
            expected_cover_letter=cover_letter.strip(),
            screening_submission=screening_submission,
        )
        if click_ready_error is not None:
            return click_ready_error

        route_pattern = "**/*vacancy_response*"

        def guard_submission_request(route: Route, request: Request) -> None:
            if not self._is_application_submission_request(request):
                route.continue_()
                return
            if self._application_submission_request_matches(
                request,
                expected_vacancy_id=vacancy_id,
                expected_resume_hh_id=expected_resume_hh_id,
            ):
                attempt.started = True
                route.continue_()
                return
            attempt.blocked = True
            route.abort("blockedbyclient")

        try:
            page.route(route_pattern, guard_submission_request)
        except PlaywrightError:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "Не удалось включить проверку фактического запроса hh.ru; кнопка не нажата",
                warnings=ready.warnings,
            )
        try:
            try:
                with page.expect_response(
                    lambda candidate: self._is_application_submission_response_for_target(
                        candidate,
                        expected_vacancy_id=vacancy_id,
                        expected_resume_hh_id=expected_resume_hh_id,
                    ),
                    timeout=min(self._timeout_ms, _SUBMISSION_RESPONSE_TIMEOUT_MS),
                ) as response_info:
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
        finally:
            with suppress(PlaywrightError):
                page.unroute(route_pattern, guard_submission_request)

        if attempt.blocked:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                (
                    "Фактический запрос hh.ru не подтвердил точные номера вакансии "
                    "и резюме; отправка отменена"
                ),
                warnings=ready.warnings,
            )

        submission_error = self._application_submission_error(page, response)
        if submission_error is not None:
            return submission_error
        confirmation = self._application_confirmation(page, response)
        if confirmation:
            return HhApplyResult(
                HhApplyStatus.APPLIED,
                page.url,
                confirmation,
                warnings=initial.warnings,
                screening_form_version_hash=(
                    screening_submission.version_hash if screening_submission is not None else None
                ),
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
                screening_form_version_hash=(
                    screening_submission.version_hash if screening_submission is not None else None
                ),
            )
        return HhApplyResult(
            HhApplyStatus.UNKNOWN_RESULT,
            page.url,
            "Кнопка нажата один раз, но hh.ru не подтвердил результат",
            warnings=initial.warnings,
            screening_form_version_hash=(
                screening_submission.version_hash if screening_submission is not None else None
            ),
        )

    @staticmethod
    def _questions_required(
        final_url: str,
        snapshot: _ApplicationSnapshot,
        confirmation: str = "",
    ) -> HhApplyResult:
        return HhApplyResult(
            HhApplyStatus.QUESTIONS_REQUIRED,
            final_url,
            confirmation,
            questions=snapshot.questions,
            warnings=snapshot.warnings,
            screening_form=snapshot.screening_form,
        )

    def _ready_application_error(
        self,
        page: Page,
        snapshot: _ApplicationSnapshot,
        *,
        vacancy_id: str,
        expected_resume_hh_id: str,
        expected_resume_title: str,
        expected_cover_letter: str,
        screening_submission: HhScreeningSubmission | None,
    ) -> HhApplyResult | None:
        vacancy_error = self._application_vacancy_error(page, vacancy_id, snapshot)
        if vacancy_error is not None:
            return vacancy_error
        if self._normalized_ui_text(snapshot.resume_title) != self._normalized_ui_text(
            expected_resume_title
        ):
            return HhApplyResult(
                HhApplyStatus.RESUME_MISMATCH,
                page.url,
                (
                    f"Перед отправкой ожидалось резюме «{expected_resume_title}», "
                    f"выбрано «{snapshot.resume_title}»"
                ),
                warnings=snapshot.warnings,
            )
        if snapshot.resume_hh_id and snapshot.resume_hh_id != expected_resume_hh_id.strip():
            return HhApplyResult(
                HhApplyStatus.RESUME_MISMATCH,
                page.url,
                (
                    f"Перед отправкой ожидалось резюме с номером "
                    f"«{expected_resume_hh_id.strip()}», "
                    f"выбрано «{snapshot.resume_hh_id}»"
                ),
                warnings=snapshot.warnings,
            )
        if snapshot.cover_letter != expected_cover_letter:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Текст письма перед отправкой отличается от подтверждённого; кнопка не нажата",
                warnings=snapshot.warnings,
            )
        if self._screening_form_is_dangerous(snapshot.screening_form):
            return self._dangerous_screening_form_result(page.url, snapshot)
        if screening_submission is None:
            return self._questions_required(page.url, snapshot) if snapshot.questions else None
        if not snapshot.questions:
            return HhApplyResult(
                HhApplyStatus.MANUAL_REVIEW_REQUIRED,
                page.url,
                "Перед отправкой ранее проверенная анкета исчезла; кнопка не нажата",
            )
        if screening_form_hash(snapshot.screening_form) != screening_submission.version_hash:
            return self._questions_required(
                page.url,
                snapshot,
                ("Состав анкеты изменился после повторного заполнения; кнопка не нажата"),
            )
        return None

    @staticmethod
    def _screening_form_is_dangerous(form: HhScreeningForm) -> bool:
        return bool(
            any(
                warning in _DANGEROUS_FORM_WARNINGS or _DANGEROUS_SCREENING_QUESTION.search(warning)
                for warning in form.warnings
            )
            or any(
                field.has_attachment
                or field.has_external_action
                or field.has_test_assignment
                or _DANGEROUS_SCREENING_QUESTION.search(field.question)
                for field in form.fields
            )
        )

    @staticmethod
    def _dangerous_screening_form_result(
        final_url: str,
        snapshot: _ApplicationSnapshot,
    ) -> HhApplyResult:
        return HhApplyResult(
            HhApplyStatus.MANUAL_REVIEW_REQUIRED,
            final_url,
            "В анкете обнаружено опасное поле или предупреждение; кнопка не нажата",
            questions=snapshot.questions,
            warnings=snapshot.warnings,
            screening_form=snapshot.screening_form,
        )

    @classmethod
    def _application_vacancy_error(
        cls,
        page: Page,
        expected_vacancy_id: str,
        snapshot: _ApplicationSnapshot | None = None,
    ) -> HhApplyResult | None:
        page_vacancy_id = cls._application_url_vacancy_id(page.url)
        form_vacancy_id = snapshot.vacancy_id if snapshot is not None else expected_vacancy_id
        if page_vacancy_id == expected_vacancy_id and form_vacancy_id == expected_vacancy_id:
            return None
        return HhApplyResult(
            HhApplyStatus.RETRYABLE_ERROR,
            page.url,
            (
                f"Ожидалась вакансия {expected_vacancy_id}, "
                f"страница или форма относится к другой вакансии; кнопка не нажата"
            ),
            warnings=snapshot.warnings if snapshot is not None else (),
        )

    def _application_access_error(
        self,
        page: Page,
        body_text: str,
    ) -> HhApplyResult | None:
        if self._contains_any(body_text, *_ACCOUNT_WARNING_MARKERS):
            return HhApplyResult(
                HhApplyStatus.ACCOUNT_WARNING,
                page.url,
                body_text[:1000],
            )
        if self._any_present(page, _CAPTCHA_SELECTOR):
            return HhApplyResult(HhApplyStatus.CAPTCHA_REQUIRED, page.url)
        if self._contains_any(body_text, *_APPLICATION_LIMIT_MARKERS):
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "hh.ru временно ограничил отправку откликов; попытка будет повторена автоматически",
                retry_after_seconds=_APPLICATION_LIMIT_RETRY_SECONDS,
            )
        if self._contains_any(body_text, *_TEMPORARY_REQUEST_LIMIT_MARKERS):
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "hh.ru временно ограничил обращения; попытка будет повторена автоматически",
                retry_after_seconds=_TEMPORARY_REQUEST_RETRY_SECONDS,
            )
        if not self.is_authenticated():
            return HhApplyResult(HhApplyStatus.AUTH_REQUIRED, page.url)
        return None

    @staticmethod
    def _application_url_vacancy_id(href: str) -> str:
        try:
            parsed = urlparse(href)
        except ValueError:
            return ""
        hostname = parsed.hostname or ""
        if hostname != "hh.ru" and not hostname.endswith(".hh.ru"):
            return ""
        query = parse_qs(parsed.query)
        query_ids = query.get("vacancyId", []) + query.get("vacancy_id", [])
        if query_ids:
            return query_ids[0] if len(query_ids) == 1 and query_ids[0].isdigit() else ""
        parts = parsed.path.strip("/").split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "vacancy" and parts[1].isdigit() else ""

    def _fill_and_verify_screening_form(
        self,
        page: Page,
        submission: HhScreeningSubmission,
    ) -> str | None:
        payload = [{"key": key, "value": value} for key, value in submission.answers]
        fill_result = page.evaluate(FILL_APPLICATION_FORM_SCRIPT, payload)
        if not isinstance(fill_result, dict):
            return "hh.ru вернул некорректный результат заполнения анкеты"
        raw_skipped = fill_result.get("skipped")
        if not isinstance(raw_skipped, list) or not all(
            isinstance(value, str) for value in raw_skipped
        ):
            return "hh.ru вернул некорректный список пропущенных полей"
        if raw_skipped:
            return "Не все подтверждённые ответы удалось подставить; кнопка не нажата"

        page.wait_for_timeout(250)
        verification = page.evaluate(VERIFY_APPLICATION_FORM_SCRIPT, payload)
        if not isinstance(verification, dict):
            return "hh.ru вернул некорректный результат проверки анкеты"
        missing = verification.get("missingRequired")
        mismatched = verification.get("mismatched")
        if (
            not isinstance(missing, list)
            or not all(isinstance(value, str) for value in missing)
            or not isinstance(mismatched, list)
            or not all(isinstance(value, str) for value in mismatched)
        ):
            return "hh.ru вернул некорректный результат проверки полей"
        if missing:
            return "В анкете остались незаполненные обязательные поля; кнопка не нажата"
        if mismatched:
            return "Значения анкеты отличаются от подтверждённых ответов; кнопка не нажата"
        return None

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
        response_url = self._application_response_url(source_url)
        try:
            response = page.goto(
                response_url,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(500)
        except PlaywrightTimeoutError:
            return HhFormReviewResult(HhFormReviewStatus.UNAVAILABLE, page.url)
        if response is not None and response.status == 429:
            return HhFormReviewResult(
                HhFormReviewStatus.UNAVAILABLE,
                page.url,
                message="hh.ru временно ограничил обращения",
            )
        if self._any_present(page, _CAPTCHA_SELECTOR):
            return HhFormReviewResult(HhFormReviewStatus.CAPTCHA_REQUIRED, page.url)
        if not self.is_authenticated():
            return HhFormReviewResult(HhFormReviewStatus.AUTH_REQUIRED, page.url)
        body_text = self._page_body_text(page)
        if self._vacancy_is_closed(response, body_text):
            return HhFormReviewResult(HhFormReviewStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhFormReviewResult(HhFormReviewStatus.ALREADY_APPLIED, page.url)
        try:
            page.locator('[data-qa="resume-title"]').first.wait_for(
                state="visible",
                timeout=self._timeout_ms,
            )
        except PlaywrightError:
            body_text = self._page_body_text(page)
            if self._vacancy_is_closed(response, body_text):
                return HhFormReviewResult(HhFormReviewStatus.VACANCY_CLOSED, page.url)
            if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
                return HhFormReviewResult(HhFormReviewStatus.ALREADY_APPLIED, page.url)
            return HhFormReviewResult(
                HhFormReviewStatus.UNAVAILABLE,
                page.url,
                message="Форма отклика hh.ru не открылась",
            )

        snapshot = self._application_snapshot(page)
        body_text = snapshot.body_text
        if self._any_present(page, _CAPTCHA_SELECTOR):
            return HhFormReviewResult(HhFormReviewStatus.CAPTCHA_REQUIRED, page.url)
        if not self.is_authenticated():
            return HhFormReviewResult(HhFormReviewStatus.AUTH_REQUIRED, page.url)
        if self._contains_any(body_text, "вакансия в архиве", "вакансия закрыта"):
            return HhFormReviewResult(HhFormReviewStatus.VACANCY_CLOSED, page.url)
        if self._contains_any(body_text, "вы уже откликались", "отклик уже отправлен"):
            return HhFormReviewResult(HhFormReviewStatus.ALREADY_APPLIED, page.url)

        try:
            snapshot = self._select_exact_resume(
                page,
                expected_resume_hh_id=expected_resume_hh_id,
                expected_resume_title=expected_resume_title,
                current_snapshot=snapshot,
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
        if self._any_present(page, _CAPTCHA_SELECTOR):
            return HhFormReviewResult(HhFormReviewStatus.CAPTCHA_REQUIRED, page.url)
        if not self.is_authenticated():
            return HhFormReviewResult(HhFormReviewStatus.AUTH_REQUIRED, page.url)
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
        current_version_hash = screening_form_hash(snapshot.screening_form)
        if (
            current_version_hash != expected_version_hash
            and expected_version_hash not in snapshot.compatible_version_hashes
        ):
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

    def current_screening_form_status(
        self,
        source_url: str,
    ) -> HhFormReviewStatus | None:
        vacancy_id, _normalized_url = self._vacancy_id_and_url(source_url)
        page = self._require_page()
        if self._application_url_vacancy_id(page.url) != vacancy_id:
            return None
        body_text = self._page_body_text(page)
        if self._vacancy_is_closed(None, body_text):
            return HhFormReviewStatus.VACANCY_CLOSED
        if self._contains_any(
            body_text,
            "вы уже откликались",
            "отклик уже отправлен",
            "отклик успешно отправлен",
            "вы откликнулись",
            "отклик принят",
        ):
            return HhFormReviewStatus.ALREADY_APPLIED
        submit_button = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit_button.count() == 1 and self._contains_any(
            submit_button.first.inner_text(),
            "повторно",
        ):
            return HhFormReviewStatus.ALREADY_APPLIED
        return None

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
        except (HhSyncBlockedError, HhSyncRetryableError):
            raise
        except RuntimeError:
            return MessageSendResult(MessageSendOutcome.FAILED)
        try:
            opened = self._open_negotiation_chat(page, vacancy_id)
        except (PlaywrightError, RuntimeError):
            return MessageSendResult(MessageSendOutcome.FAILED)
        if opened is not True:
            return MessageSendResult(MessageSendOutcome.FAILED)
        frame = self._wait_for_chat_frame(page)
        if frame is None:
            return MessageSendResult(MessageSendOutcome.FAILED)

        editor = frame.locator('[data-qa="chatik-new-message-text"]')
        submit = frame.locator('[data-qa="chatik-do-send-message"]')
        if editor.count() != 1 or submit.count() != 1 or not editor.first.is_enabled():
            return MessageSendResult(MessageSendOutcome.FAILED)
        before = self._outgoing_messages_snapshot(frame, vacancy_id, exact_body)
        if before is None:
            return MessageSendResult(MessageSendOutcome.FAILED)
        editor.first.fill(exact_body)
        for _attempt in range(4):
            if submit.first.is_enabled():
                break
            page.wait_for_timeout(250)
        else:
            return MessageSendResult(MessageSendOutcome.FAILED)

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
            pass

        self._raise_message_submission_error(page, response)
        after = self._outgoing_messages_snapshot(frame, vacancy_id, exact_body)
        if after is not None:
            new_ids = after.message_ids - before.message_ids
            if new_ids or after.exact_count > before.exact_count:
                external_id = min(new_ids) if new_ids else self._message_external_id(response)
                return MessageSendResult(
                    MessageSendOutcome.SENT,
                    external_id,
                )
        if response is not None and 400 <= response.status < 500:
            return MessageSendResult(MessageSendOutcome.FAILED)
        return MessageSendResult(MessageSendOutcome.UNKNOWN_RESULT)

    def _open_negotiation_chat(self, page: Page, vacancy_id: str) -> bool:
        if page.evaluate(OPEN_NEGOTIATION_CHAT_SCRIPT, vacancy_id) is True:
            return True
        for page_number in self._negotiation_page_numbers(page):
            self._select_negotiation_page(page, page_number)
            if page.evaluate(OPEN_NEGOTIATION_CHAT_SCRIPT, vacancy_id) is True:
                return True
        return False

    @staticmethod
    def _negotiation_page_numbers(page: Page) -> tuple[int, ...]:
        numbers: set[int] = set()
        for button in page.locator('[data-qa^="number-pages-"]').all():
            value = button.inner_text().strip()
            if value.isdigit() and int(value) > 0:
                numbers.add(int(value))
        return tuple(sorted(numbers))

    def _select_negotiation_page(self, page: Page, page_number: int) -> None:
        selected = page.locator('[data-qa*="number-pages-selected"]')
        if selected.count() == 1 and selected.first.inner_text().strip() == str(page_number):
            return
        button = page.locator(f'[data-qa^="number-pages-{page_number}"]')
        if button.count() != 1:
            raise RuntimeError(f"hh.ru не показал страницу откликов {page_number}")
        try:
            button.first.click(no_wait_after=True)
            page.wait_for_timeout(1_500)
        except PlaywrightError as error:
            raise RuntimeError(f"Не удалось открыть страницу откликов {page_number}") from error

    def _raise_message_submission_error(
        self,
        page: Page,
        response: Response | None,
    ) -> None:
        response_text = ""
        if response is not None:
            with suppress(PlaywrightError):
                response_text = response.text()
        combined_text = f"{response_text}\n{self._page_body_text(page)}"
        if response is not None and response.status == 403:
            raise HhSyncBlockedError(
                "ACCOUNT_WARNING",
                "hh.ru отклонил сообщение и запросил ручную проверку аккаунта",
            )
        if self._contains_any(combined_text, *_ACCOUNT_WARNING_MARKERS):
            raise HhSyncBlockedError(
                "ACCOUNT_WARNING",
                "hh.ru показал предупреждение безопасности",
            )
        if response is not None and response.status == 429:
            raise HhSyncRetryableError(
                "HH_RATE_LIMITED",
                "hh.ru временно ограничил отправку сообщений",
                retry_after_seconds=(
                    self._retry_after_seconds(response) or _TEMPORARY_REQUEST_RETRY_SECONDS
                ),
            )
        if self._contains_any(combined_text, *_TEMPORARY_REQUEST_LIMIT_MARKERS):
            raise HhSyncRetryableError(
                "HH_RATE_LIMITED",
                "hh.ru временно ограничил отправку сообщений",
                retry_after_seconds=_TEMPORARY_REQUEST_RETRY_SECONDS,
            )

    @staticmethod
    def _outgoing_messages_snapshot(
        frame: Frame,
        vacancy_id: str,
        exact_body: str,
    ) -> _OutgoingMessagesSnapshot | None:
        try:
            payload = frame.evaluate(CHAT_MESSAGES_SCRIPT, vacancy_id)
        except PlaywrightError:
            return None
        if not isinstance(payload, list):
            return None

        message_ids: set[str] = set()
        exact_count = 0
        for item in payload:
            if not isinstance(item, dict):
                return None
            direction = item.get("direction")
            message_body = item.get("body")
            if not isinstance(direction, str) or not isinstance(message_body, str):
                return None
            if direction != MessageDirection.OUTGOING.value or message_body != exact_body:
                continue
            exact_count += 1
            message_id = item.get("messageId")
            if isinstance(message_id, (int, str)) and str(message_id).strip():
                message_ids.add(str(message_id))
        return _OutgoingMessagesSnapshot(exact_count, frozenset(message_ids))

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
            return int(stripped)
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(ceil((retry_at - datetime.now(UTC)).total_seconds()), 0)

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
            response = page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3_000)
        except PlaywrightError as error:
            raise RuntimeError("Страница откликов hh.ru не загрузилась") from error
        if response is not None and response.status == 429:
            raise HhSyncRetryableError(
                "HH_RATE_LIMITED",
                "hh.ru временно ограничил обращения; проверка будет повторена автоматически",
                retry_after_seconds=(
                    self._retry_after_seconds(response) or _TEMPORARY_REQUEST_RETRY_SECONDS
                ),
            )
        if response is not None and response.status == 403:
            raise HhSyncBlockedError(
                "ACCOUNT_WARNING",
                "hh.ru запретил доступ к странице; требуется ручная проверка аккаунта",
            )
        body_text = self._page_body_text(page)
        if self._contains_any(body_text, *_ACCOUNT_WARNING_MARKERS):
            raise HhSyncBlockedError(
                "ACCOUNT_WARNING",
                "hh.ru показал предупреждение аккаунта",
            )
        if self._contains_any(body_text, *_TEMPORARY_REQUEST_LIMIT_MARKERS):
            raise HhSyncRetryableError(
                "HH_RATE_LIMITED",
                "hh.ru временно ограничил обращения; проверка будет повторена автоматически",
                retry_after_seconds=_TEMPORARY_REQUEST_RETRY_SECONDS,
            )
        if self._any_present(page, _CAPTCHA_SELECTOR):
            raise HhSyncBlockedError("CAPTCHA_REQUIRED", "hh.ru запросил проверку")
        if not self.is_authenticated():
            raise HhSyncBlockedError("AUTH_REQUIRED", "Требуется повторный вход в hh.ru")
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

    @classmethod
    def _is_application_submission_response_for_target(
        cls,
        response: Response,
        *,
        expected_vacancy_id: str,
        expected_resume_hh_id: str,
    ) -> bool:
        if not cls._is_application_submission_response(response):
            return False
        try:
            request = response.request
        except (AttributeError, PlaywrightError):
            return False
        return cls._application_submission_request_matches(
            request,
            expected_vacancy_id=expected_vacancy_id,
            expected_resume_hh_id=expected_resume_hh_id,
            request_url=response.url,
        )

    @staticmethod
    def _is_application_submission_request(request: Request) -> bool:
        try:
            method = request.method.upper()
            parsed = urlparse(request.url)
        except (AttributeError, PlaywrightError, ValueError):
            return False
        return (
            method == "POST"
            and (parsed.hostname == "hh.ru" or (parsed.hostname or "").endswith(".hh.ru"))
            and "vacancy_response" in parsed.path
        )

    @classmethod
    def _application_submission_request_matches(
        cls,
        request: Request,
        *,
        expected_vacancy_id: str,
        expected_resume_hh_id: str,
        request_url: str | None = None,
    ) -> bool:
        identifiers = cls._submission_identifiers(
            request,
            request_url=request_url,
        )
        if identifiers is None:
            return False
        vacancy_ids, resume_ids = identifiers
        return vacancy_ids == frozenset((expected_vacancy_id.strip(),)) and resume_ids == frozenset(
            (expected_resume_hh_id.strip(),)
        )

    @staticmethod
    def _submission_identifiers(
        request: Request,
        *,
        request_url: str | None = None,
    ) -> tuple[frozenset[str], frozenset[str]] | None:
        vacancy_ids: set[str] = set()
        resume_ids: set[str] = set()
        invalid = False

        def add_vacancy(value: object) -> None:
            nonlocal invalid
            if isinstance(value, int):
                candidate = str(value)
            elif isinstance(value, str):
                candidate = value.strip()
            else:
                invalid = True
                return
            if not candidate.isdigit():
                invalid = True
                return
            vacancy_ids.add(candidate)

        def add_resume(value: object) -> None:
            nonlocal invalid
            if isinstance(value, int):
                candidate = str(value)
            elif isinstance(value, str):
                candidate = value.strip()
            else:
                invalid = True
                return
            if not candidate or len(candidate) > 255:
                invalid = True
                return
            resume_ids.add(candidate)

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized_key = (
                        key.replace("_", "").replace("-", "").casefold()
                        if isinstance(key, str)
                        else ""
                    )
                    if normalized_key == "vacancyid":
                        add_vacancy(nested)
                    elif normalized_key in {"resumeid", "resumehash"}:
                        add_resume(nested)
                    else:
                        collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        try:
            parsed = urlparse(request_url or request.url)
            query = parse_qs(parsed.query)
            for key, values in query.items():
                normalized_key = key.replace("_", "").replace("-", "").casefold()
                if normalized_key == "vacancyid":
                    for value in values:
                        add_vacancy(value)
                elif normalized_key in {"resumeid", "resumehash"}:
                    for value in values:
                        add_resume(value)
            post_data = request.post_data
        except (AttributeError, PlaywrightError, ValueError):
            return None

        if isinstance(post_data, str) and post_data.strip():
            try:
                collect(json.loads(post_data))
            except json.JSONDecodeError:
                for key, values in parse_qs(post_data).items():
                    normalized_key = key.replace("_", "").replace("-", "").casefold()
                    if normalized_key == "vacancyid":
                        for value in values:
                            add_vacancy(value)
                    elif normalized_key in {"resumeid", "resumehash"}:
                        for value in values:
                            add_resume(value)
        if invalid:
            return None
        return frozenset(vacancy_ids), frozenset(resume_ids)

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

    def _application_submission_error(
        self,
        page: Page,
        response: Response | None,
    ) -> HhApplyResult | None:
        response_text = ""
        if response is not None:
            with suppress(PlaywrightError):
                response_text = response.text()
        page_text = self._page_body_text(page)
        combined_text = f"{response_text}\n{page_text}"
        if response is not None and response.status == 403:
            return HhApplyResult(
                HhApplyStatus.ACCOUNT_WARNING,
                page.url,
                "hh.ru отклонил отправку и запросил ручную проверку аккаунта",
            )
        if self._contains_any(combined_text, *_ACCOUNT_WARNING_MARKERS):
            return HhApplyResult(
                HhApplyStatus.ACCOUNT_WARNING,
                page.url,
                "hh.ru показал предупреждение безопасности; требуется ручная проверка аккаунта",
            )
        if response is not None and response.status == 429:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "hh.ru временно ограничил отправку; отклик будет повторён автоматически",
                retry_after_seconds=(
                    self._retry_after_seconds(response) or _TEMPORARY_REQUEST_RETRY_SECONDS
                ),
            )
        if self._contains_any(combined_text, *_APPLICATION_LIMIT_MARKERS):
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "hh.ru временно ограничил отправку откликов; попытка будет повторена автоматически",
                retry_after_seconds=_APPLICATION_LIMIT_RETRY_SECONDS,
            )
        if self._contains_any(combined_text, *_TEMPORARY_REQUEST_LIMIT_MARKERS):
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                "hh.ru временно ограничил обращения; попытка будет повторена автоматически",
                retry_after_seconds=_TEMPORARY_REQUEST_RETRY_SECONDS,
            )
        return None

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
        if not is_hh or "/account/login" in parsed_url.path:
            return False
        if self._any_present(page, _CAPTCHA_SELECTOR):
            return False
        if self._any_present(page, _CONFIRMATION_CODE_SELECTOR):
            return False
        if self.has_account_warning():
            return False
        return parsed_url.path.startswith("/applicant/") or self._any_present(
            page,
            _AUTHENTICATED_APPLICANT_SELECTOR,
        )

    def authentication_status(self) -> LoginStatus:
        return self._classify(self._require_page())

    def has_account_warning(self) -> bool:
        body_text = self._page_body_text(self._require_page())
        return self._contains_any(body_text, *_ACCOUNT_WARNING_MARKERS)

    def wait_for_authentication(self) -> bool:
        page = self._require_page()
        deadline = monotonic() + self._timeout_ms / 1000
        while not page.is_closed():
            if self.has_account_warning():
                return False
            if self.is_authenticated():
                return True
            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            page.wait_for_timeout(min(remaining_ms, 500))
        return False

    def submit_credentials(self, credentials: HhCredentials) -> LoginStatus:
        page = self._require_page()
        current_status = self._wait_for_login_surface(page)
        if current_status is not LoginStatus.MANUAL_ACTION_REQUIRED:
            return current_status
        self._ensure_secure_hh_login_target(page)
        try:
            self._open_applicant_form(page)
            current_status = self._classify(page)
            if current_status is not LoginStatus.MANUAL_ACTION_REQUIRED:
                return current_status
            self._ensure_secure_hh_login_target(page)
            self._fill_login(page, credentials.login.strip())
            self._click_unique(page.locator('[data-qa="expand-login-by-password"]'))

            password = page.locator(
                '[data-qa="applicant-login-input-password"], '
                '[data-qa="account-login-password"], input[name="password"]'
            )
            password.wait_for(state="visible", timeout=self._timeout_ms)
        except PlaywrightTimeoutError as error:
            current_status = self._classify(page)
            if current_status is not LoginStatus.MANUAL_ACTION_REQUIRED:
                return current_status
            raise HhSyncRetryableError(
                "HH_LOGIN_FORM_TIMEOUT",
                "Форма входа hh.ru не успела загрузиться; проверка будет повторена автоматически",
                retry_after_seconds=_NETWORK_RETRY_SECONDS,
            ) from error

        self._ensure_secure_hh_login_target(page)
        password.fill(credentials.password)
        self._click_unique(page.locator('[data-qa="submit-button"]'))
        page.wait_for_timeout(1_000)
        return self._classify(page)

    @staticmethod
    def _ensure_secure_hh_login_target(page: Page) -> None:
        parsed = urlparse(page.url)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme.casefold() == "https" and (
            hostname == "hh.ru" or hostname.endswith(".hh.ru")
        ):
            return
        raise HhSyncBlockedError(
            "UNSAFE_LOGIN_TARGET",
            "Сохранённые данные входа разрешено вводить только на защищённой странице hh.ru",
        )

    def _wait_for_login_surface(self, page: Page) -> LoginStatus:
        attempts = max(1, min(self._timeout_ms, 10_000) // 500)
        for _attempt in range(attempts):
            status = self._classify(page)
            if status is not LoginStatus.MANUAL_ACTION_REQUIRED:
                return status
            if any(page.locator(selector).count() > 0 for selector in _LOGIN_SURFACE_SELECTORS):
                return status
            page.wait_for_timeout(500)
        raise HhSyncRetryableError(
            "HH_LOGIN_FORM_TIMEOUT",
            "Страница входа hh.ru не успела загрузиться; проверка будет повторена автоматически",
            retry_after_seconds=_NETWORK_RETRY_SECONDS,
        )

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
        if self.has_account_warning():
            return LoginStatus.ACCOUNT_WARNING
        if self._any_present(page, _CAPTCHA_SELECTOR):
            return LoginStatus.CAPTCHA_REQUIRED
        if self._any_present(page, _CONFIRMATION_CODE_SELECTOR):
            return LoginStatus.CONFIRMATION_REQUIRED
        if self.is_authenticated():
            return LoginStatus.AUTHENTICATED
        if self._any_visible(page, '[data-qa="form-helper-error"]'):
            return LoginStatus.INVALID_CREDENTIALS
        return LoginStatus.MANUAL_ACTION_REQUIRED

    @staticmethod
    def _any_visible(page: Page, selector: str) -> bool:
        locators = page.locator(selector)
        return any(locator.is_visible() for locator in locators.all())

    @staticmethod
    def _any_present(page: Page, selector: str) -> bool:
        return page.locator(selector).count() > 0

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
        current_snapshot: _ApplicationSnapshot | None = None,
    ) -> _ApplicationSnapshot:
        resume_hh_id = expected_resume_hh_id.strip()
        resume_title = expected_resume_title.strip()
        if not resume_hh_id or not resume_title:
            raise _ResumeSelectionError(
                "У назначенного резюме отсутствует номер или название",
                retryable=False,
            )

        if (
            current_snapshot is not None
            and self._normalized_ui_text(current_snapshot.resume_title)
            == self._normalized_ui_text(resume_title)
            and current_snapshot.resume_hh_id in {"", resume_hh_id}
        ):
            return current_snapshot

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
        global_dropdown_selector = (
            '[role="option"][data-qa^="magritte-select-option-"]'
        )
        options: list[Locator] = []
        uses_bottom_sheet = False
        option_attempts = max(
            1,
            ceil(min(self._timeout_ms, _RESUME_OPTIONS_TIMEOUT_MS) / 500),
        )
        for opening_attempt in range(2):
            if opening_attempt:
                try:
                    selected_card.first.click(
                        force=True,
                        no_wait_after=True,
                        timeout=min(self._timeout_ms, 10_000),
                    )
                except PlaywrightError as error:
                    raise _ResumeSelectionError(
                        "hh.ru повторно не открыл список резюме для отклика",
                        retryable=True,
                    ) from error
            for _attempt in range(option_attempts):
                page.wait_for_timeout(500)
                options = page.locator(bottom_sheet_selector).all()
                uses_bottom_sheet = bool(options)
                if not options:
                    options = page.locator(dropdown_selector).all()
                if not options:
                    options = page.locator(global_dropdown_selector).all()
                if options:
                    break
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
        if snapshot.resume_hh_id != resume_hh_id:
            raise _ResumeSelectionError(
                (
                    f"Ожидалось резюме с номером «{resume_hh_id}», "
                    f"после выбора определено «{snapshot.resume_hh_id or 'не определено'}»"
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
        legacy_fields: list[HhScreeningField] = []
        has_detached_controls = False
        for raw_field in raw_fields:
            raw_options = raw_field.get("options", [])
            if not isinstance(raw_options, list) or not all(
                isinstance(option, str) for option in raw_options
            ):
                raise RuntimeError("hh.ru вернул некорректные варианты ответа")
            raw_max_length = raw_field.get("maxLength")
            if raw_max_length is not None and not isinstance(raw_max_length, int):
                raise RuntimeError("hh.ru вернул некорректное ограничение длины")
            field = HhScreeningField(
                key=self._required_string(raw_field, "key", "ключа вопроса"),
                question=self._required_string(raw_field, "question", "текста вопроса"),
                field_type=self._required_string(raw_field, "fieldType", "типа вопроса"),
                is_required=raw_field.get("isRequired") is not False,
                options=tuple(option.strip() for option in raw_options if option.strip()),
                max_length=raw_max_length,
                format_hint=self._optional_string(raw_field, "formatHint"),
                has_attachment=raw_field.get("hasAttachment") is True,
                has_external_action=raw_field.get("hasExternalAction") is True,
                has_test_assignment=raw_field.get("hasTestAssignment") is True,
            )
            fields.append(field)
            if raw_field.get("controlOutsideQuestion") is True:
                has_detached_controls = True
                legacy_fields.append(
                    HhScreeningField(
                        key=field.key,
                        question=field.question,
                        field_type="unknown",
                        is_required=bool(re.search(r"(^|\s)\*(\s|$)", field.question)),
                    )
                )
            else:
                legacy_fields.append(field)
        screening_form = HhScreeningForm(
            fields=tuple(fields),
            warnings=tuple(item.strip() for item in raw_warnings if item.strip()),
        )
        raw_cover_letter = payload.get("coverLetter")
        if not isinstance(raw_cover_letter, str):
            raise RuntimeError("hh.ru вернул некорректный текст сопроводительного письма")
        vacancy_id = self._optional_string(payload, "vacancyId")
        if vacancy_id and not vacancy_id.isdigit():
            raise RuntimeError("hh.ru вернул некорректный номер вакансии в форме отклика")
        compatible_version_hashes = (
            (screening_form_hash(HhScreeningForm(fields=tuple(legacy_fields))),)
            if has_detached_controls
            else ()
        )
        return _ApplicationSnapshot(
            screening_form=screening_form,
            resume_title=self._optional_string(payload, "resumeTitle"),
            resume_hh_id=self._optional_string(payload, "resumeHhId"),
            cover_letter=raw_cover_letter,
            body_text=self._optional_string(payload, "bodyText"),
            vacancy_id=vacancy_id,
            compatible_version_hashes=compatible_version_hashes,
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
        except ValueError:
            normalized = " ".join(value.casefold().replace("\u00a0", " ").split())
            local_now = datetime.now().astimezone()
            if "сегодня" in normalized:
                parsed = local_now.replace(hour=12, minute=0, second=0, microsecond=0)
            elif "вчера" in normalized:
                parsed = (local_now - timedelta(days=1)).replace(
                    hour=12,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            else:
                months = {
                    "января": 1,
                    "февраля": 2,
                    "марта": 3,
                    "апреля": 4,
                    "мая": 5,
                    "июня": 6,
                    "июля": 7,
                    "августа": 8,
                    "сентября": 9,
                    "октября": 10,
                    "ноября": 11,
                    "декабря": 12,
                }
                match = re.search(
                    r"\b(\d{1,2})\s+(" + "|".join(months) + r")(?:\s+(20\d{2}))?\b",
                    normalized,
                )
                if match is None:
                    raise RuntimeError("hh.ru вернул некорректную дату вакансии") from None
                explicit_year = match.group(3)
                year = int(explicit_year or local_now.year)
                try:
                    parsed = local_now.replace(
                        year=year,
                        month=months[match.group(2)],
                        day=int(match.group(1)),
                        hour=12,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                except ValueError:
                    if explicit_year is not None:
                        raise RuntimeError("hh.ru вернул некорректную дату вакансии") from None
                    parsed = local_now.replace(
                        year=year - 1,
                        month=months[match.group(2)],
                        day=int(match.group(1)),
                        hour=12,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                if explicit_year is None and parsed > local_now + timedelta(days=1):
                    parsed = parsed.replace(year=parsed.year - 1)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    @classmethod
    def _vacancy_availability(
        cls,
        response: Response | None,
        payload: object,
    ) -> VacancyAvailability:
        if response is not None and response.status in {404, 410}:
            return VacancyAvailability.UNAVAILABLE
        if not isinstance(payload, dict):
            return VacancyAvailability.ACTIVE
        raw = cls._optional_string(payload, "availability") or VacancyAvailability.ACTIVE.value
        try:
            return VacancyAvailability(raw)
        except ValueError as error:
            raise RuntimeError("hh.ru вернул некорректное состояние вакансии") from error

    @classmethod
    def _vacancy_is_closed(cls, response: Response | None, body_text: str) -> bool:
        if response is not None and response.status in {404, 410}:
            return True
        return cls._contains_any(
            body_text,
            "вакансия в архиве",
            "вакансия закрыта",
            "вакансия недоступна",
            "недоступна эта вакансия",
            "вакансия не найдена",
            "такой вакансии нет",
        )

    @staticmethod
    def _page_body_text(page: Page) -> str:
        try:
            return page.locator("body").inner_text()
        except PlaywrightError:
            return ""

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
