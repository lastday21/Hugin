from __future__ import annotations

import re
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

from hugin.domain.hh import (
    HhApplyResult,
    HhApplyStatus,
    HhFormReviewResult,
    HhFormReviewStatus,
    HhProfileData,
    HhResumeData,
    HhResumeDetails,
    HhScreeningField,
    HhScreeningForm,
    screening_form_hash,
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
() => ({
    title: (
        document.querySelector('[data-qa="resume-block-title-position"]')?.textContent || ''
    ).trim(),
    experience: (
        document.querySelector('[data-qa="resume-list-card-experience"]')?.innerText || ''
    ).trim(),
    skills: (
        document.querySelector('[data-qa="skills-card"]')?.innerText || ''
    ).trim(),
    education: (
        document.querySelector('[data-qa="resume-list-card-education"]')?.innerText || ''
    ).trim(),
})
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


class VisibleHhBrowser:
    def __init__(
        self,
        profile_dir: Path,
        login_url: str,
        resumes_url: str,
        search_url: str,
        timeout_ms: int,
    ) -> None:
        self._profile_dir = profile_dir
        self._login_url = login_url
        self._resumes_url = resumes_url
        self._search_url = search_url
        self._timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> VisibleHhBrowser:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir),
                headless=False,
                no_viewport=True,
                args=["--start-maximized"],
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self._timeout_ms)
            self._page.set_default_navigation_timeout(self._timeout_ms)
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        return self

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
        return HhResumeDetails(
            hh_id=resume_id,
            title=self._required_string(payload, "title", "названия резюме"),
            experience=self._optional_string(payload, "experience"),
            skills=self._optional_string(payload, "skills"),
            education=self._optional_string(payload, "education"),
        )

    def apply_to_vacancy(
        self,
        source_url: str,
        *,
        expected_resume_title: str,
        cover_letter: str,
    ) -> HhApplyResult:
        vacancy_id, normalized_url = self._vacancy_id_and_url(source_url)
        parsed = urlparse(normalized_url)
        response_url = self._application_response_url(source_url)
        page = self._require_page()
        try:
            initial_response = page.goto(response_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
        if initial_response is not None and initial_response.status == 429:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                retry_after_seconds=self._retry_after_seconds(initial_response),
            )

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
        submit = page.locator('[data-qa="vacancy-response-submit-popup"]')
        if submit.count() == 1 and self._contains_any(submit.first.inner_text(), "повторно"):
            return HhApplyResult(HhApplyStatus.ALREADY_APPLIED, page.url, body_text[:1000])
        if initial.questions:
            return HhApplyResult(
                HhApplyStatus.QUESTIONS_REQUIRED,
                page.url,
                questions=initial.questions,
                warnings=initial.warnings,
                screening_form=initial.screening_form,
            )
        if initial.resume_title != expected_resume_title.strip():
            return HhApplyResult(
                HhApplyStatus.RESUME_MISMATCH,
                page.url,
                confirmation=(
                    f"Ожидалось резюме «{expected_resume_title}», выбрано «{initial.resume_title}»"
                ),
                warnings=initial.warnings,
            )

        if cover_letter.strip():
            letter = page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
            if letter.count() == 0:
                toggle = page.locator('[data-qa="vacancy-response-letter-toggle"]')
                if toggle.count() != 1:
                    return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)
                toggle.click()
            letter.first.wait_for(state="visible", timeout=self._timeout_ms)
            letter.first.fill(cover_letter.strip())

        if submit.count() != 1 or not submit.first.is_enabled():
            return HhApplyResult(HhApplyStatus.RETRYABLE_ERROR, page.url)

        try:
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and "/applicant/vacancy_response" in response.url
                ),
                timeout=self._timeout_ms,
            ) as response_info:
                submit.first.click(no_wait_after=True)
            response = response_info.value
        except PlaywrightTimeoutError:
            return HhApplyResult(HhApplyStatus.UNKNOWN_RESULT, page.url)

        try:
            page.wait_for_timeout(1_500)
            confirmation = self._response_confirmation(response.status, response.text())
            final_body = page.locator("body").inner_text().strip()
        except PlaywrightError:
            confirmation = self._response_confirmation(response.status, "")
            final_body = ""
        if response.status == 429:
            return HhApplyResult(
                HhApplyStatus.RETRYABLE_ERROR,
                page.url,
                confirmation,
                warnings=initial.warnings,
                retry_after_seconds=self._retry_after_seconds(response),
            )
        if self._contains_any(final_body, "вы уже откликались", "отклик уже отправлен"):
            return HhApplyResult(
                HhApplyStatus.ALREADY_APPLIED,
                page.url,
                confirmation,
                warnings=initial.warnings,
            )
        if (
            "/applicant/negotiations" in page.url
            or self._contains_any(
                final_body,
                "отклик отправлен",
                "вы откликнулись",
                "резюме доставлено",
            )
            or self._contains_any(confirmation, '"success":true', '"status":"ok"')
        ):
            return HhApplyResult(
                HhApplyStatus.APPLIED,
                page.url,
                confirmation,
                warnings=initial.warnings,
            )
        if 200 <= response.status < 300 and self._vacancy_in_negotiations(
            page,
            parsed.scheme,
            parsed.netloc,
            vacancy_id,
        ):
            return HhApplyResult(
                HhApplyStatus.APPLIED,
                page.url,
                f"{confirmation}; подтверждено в списке откликов",
                warnings=initial.warnings,
            )
        return HhApplyResult(
            HhApplyStatus.UNKNOWN_RESULT,
            page.url,
            confirmation,
            warnings=initial.warnings,
        )

    def open_screening_form(
        self,
        source_url: str,
        *,
        expected_resume_title: str,
        expected_version_hash: str,
        answers: dict[str, str],
        cover_letter: str = "",
    ) -> HhFormReviewResult:
        page = self._require_page()
        try:
            response = page.goto(
                self._application_response_url(source_url),
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
        if snapshot.resume_title != expected_resume_title.strip():
            return HhFormReviewResult(
                HhFormReviewStatus.RESUME_MISMATCH,
                page.url,
                current_form=snapshot.screening_form,
                message=(
                    f"Ожидалось резюме «{expected_resume_title}», выбрано «{snapshot.resume_title}»"
                ),
            )
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
            page.wait_for_timeout(1_500)
            links = page.locator('a[href*="/vacancy/"]')
            return any(
                self._vacancy_id_from_href(link.get_attribute("href")) == vacancy_id
                for link in links.all()
            )
        except PlaywrightError:
            return False

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
        parts = urlparse(href).path.strip("/").split("/")
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
