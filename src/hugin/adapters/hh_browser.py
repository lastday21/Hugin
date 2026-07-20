from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from urllib.parse import urlencode, urlparse, urlunparse

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from hugin.domain.hh import HhProfileData, HhResumeData
from hugin.domain.vacancies import VacancyData, VacancySearchResult
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
    })),
})
"""

VACANCY_DETAILS_SCRIPT = """
() => ({
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
        document.querySelector('[data-qa="vacancy-description"]')?.innerText || ''
    ).trim(),
    skills: Array.from(document.querySelectorAll('[data-qa="skills-element"]'))
        .map((element) => (element.textContent || '').trim())
        .filter(Boolean),
})
"""

ALLOWED_SEARCH_FILTERS = frozenset(
    {
        "currency",
        "employment",
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
        self._require_page().goto(self._login_url, wait_until="domcontentloaded")

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
            vacancy_id, source_url = self._vacancy_id_and_url(href)
            vacancies.append(
                VacancyData(
                    hh_id=vacancy_id,
                    title=title,
                    source_url=source_url,
                    employer_name=employer,
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

        return VacancyData(
            hh_id=vacancy_id,
            title=self._required_string(payload, "title", "названия вакансии"),
            source_url=normalized_url,
            employer_name=self._optional_string(payload, "employer") or None,
            description=self._optional_string(payload, "description") or None,
            experience=self._optional_string(payload, "experience") or None,
            employment=self._optional_string(payload, "employment") or None,
            work_format=self._optional_string(payload, "workFormat") or None,
            key_skills=tuple(skill.strip() for skill in raw_skills if skill.strip()),
            details_fetched_at=datetime.now(UTC),
        )

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
