from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Error, Locator, Page, TimeoutError

from hugin.adapters import hh_browser as browser_module
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.domain.hh import HhApplyStatus, HhProfileData, HhResumeData, HhResumeDetails
from hugin.services.hh_login import HhCredentials, LoginStatus


class FakeLocator:
    def __init__(
        self,
        count: int = 1,
        *,
        checked: bool = False,
        visible: bool = False,
        wait_error: bool = False,
        enabled: bool = True,
        text: str = "",
        href: str | None = None,
    ) -> None:
        self._count = count
        self.checked = checked
        self.visible = visible
        self.wait_error = wait_error
        self.enabled = enabled
        self.text = text
        self.href = href
        self.clicked = 0
        self.no_wait_after: list[bool] = []
        self.filled: list[str] = []

    def count(self) -> int:
        return self._count

    def is_checked(self) -> bool:
        return self.checked

    def check(self, *, force: bool = False) -> None:
        assert force
        self.checked = True

    def click(self, *, no_wait_after: bool = False) -> None:
        self.clicked += 1
        self.no_wait_after.append(no_wait_after)

    def fill(self, value: str) -> None:
        self.filled.append(value)

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state in {"attached", "visible"}
        assert timeout > 0
        if self.wait_error:
            raise TimeoutError("wait")

    def all(self) -> list[FakeLocator]:
        return [self] if self._count else []

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None

    @property
    def first(self) -> FakeLocator:
        return self


class FakePage:
    def __init__(self, url: str = "https://hh.ru/account/login?role=applicant") -> None:
        self.url = url
        self.locators: dict[str, FakeLocator] = {}
        self.goto_calls: list[tuple[str, str]] = []
        self.timeout: int | None = None
        self.navigation_timeout: int | None = None
        self.profile_payload: object = None
        self.search_payload: object = None
        self.details_payload: object = None
        self.resume_payload: object = None
        self.application_payload: object = None
        self.response = FakeResponse()
        self.goto_error: Error | None = None

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator(0))

    def goto(self, url: str, *, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def wait_for_timeout(self, timeout: int) -> None:
        assert timeout in {500, 1_000, 1_500}

    def evaluate(self, expression: str) -> object:
        if "ResumeProfileFront-InitialState" in expression:
            return self.profile_payload
        if "vacancy-serp__vacancy" in expression:
            return self.search_payload
        if "vacancy-description" in expression:
            return self.details_payload
        if "resume-block-title-position" in expression:
            return self.resume_payload
        if "task-question" in expression:
            return self.application_payload
        raise AssertionError("unexpected browser script")

    def expect_response(self, predicate: object, *, timeout: int) -> FakeResponseInfo:
        assert timeout > 0
        assert callable(predicate)
        assert predicate(self.response)
        return FakeResponseInfo(self.response)


class FakeRequest:
    method = "POST"


class FakeResponse:
    def __init__(self) -> None:
        self.request = FakeRequest()
        self.url = "https://hh.ru/applicant/vacancy_response?vacancyId=123"
        self.status = 200
        self.text_error: Error | None = None
        self.body = '{"success":true}'

    def text(self) -> str:
        if self.text_error is not None:
            raise self.text_error
        return self.body


class FakeResponseInfo:
    def __init__(self, response: FakeResponse) -> None:
        self.value = response

    def __enter__(self) -> FakeResponseInfo:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def make_browser(page: FakePage, tmp_path: Path) -> VisibleHhBrowser:
    browser = VisibleHhBrowser(
        tmp_path,
        "https://hh.ru/account/login?role=applicant",
        "https://hh.ru/applicant/resumes",
        "https://hh.ru/search/vacancy",
        5_000,
    )
    browser._page = cast(Page, page)
    return browser


def prepare_login_page(page: FakePage, *, password_error: bool = False) -> None:
    page.locators.update(
        {
            '[data-qa="applicant-login-card"]': FakeLocator(),
            '[data-qa^="account-type-card-APPLICANT"]': FakeLocator(checked=False),
            '[data-qa="expand-login-by-password"]': FakeLocator(),
            '[data-qa^="credential-type-EMAIL"]': FakeLocator(checked=False),
            '[data-qa="applicant-login-input-email"]': FakeLocator(),
            (
                '[data-qa="applicant-login-input-password"], '
                '[data-qa="account-login-password"], input[name="password"]'
            ): FakeLocator(wait_error=password_error),
            '[data-qa="submit-button"]': FakeLocator(),
            '[data-qa*="captcha"], iframe[src*="captcha"]': FakeLocator(0),
            ('[data-qa*="otp"], [data-qa*="verification-code"], input[name*="code"]'): FakeLocator(
                0
            ),
            '[data-qa="form-helper-error"]': FakeLocator(0),
        }
    )


def test_browser_opens_login_page_and_detects_session(tmp_path: Path) -> None:
    page = FakePage()
    browser = make_browser(page, tmp_path)

    browser.open_login()

    assert page.goto_calls == [("https://hh.ru/account/login?role=applicant", "domcontentloaded")]
    assert not browser.is_authenticated()
    page.url = "https://hh.ru/applicant/resumes"
    assert browser.is_authenticated()
    page.url = "https://ufa.hh.ru/applicant/resumes"
    assert browser.is_authenticated()
    page.url = "https://not-hh.ru/applicant/resumes"
    assert not browser.is_authenticated()


def test_aborted_login_redirect_is_accepted_for_authenticated_page(tmp_path: Path) -> None:
    page = FakePage("https://ufa.hh.ru/applicant/resumes")
    page.goto_error = Error("net::ERR_ABORTED")
    browser = make_browser(page, tmp_path)

    browser.open_login()

    assert browser.is_authenticated()


def test_profile_and_resumes_are_read_from_page(tmp_path: Path) -> None:
    page = FakePage()
    page.profile_payload = {
        "externalId": "12345",
        "firstName": "Иван",
        "lastName": "Иванов",
        "resumes": [
            {
                "title": "Python-разработчик",
                "href": "https://ufa.hh.ru/resume/first-resume?hhtmFrom=resume_list",
            },
            {
                "title": "Инженер",
                "href": "https://hh.ru/resume/second-resume",
            },
        ],
    }
    browser = make_browser(page, tmp_path)

    profile = browser.read_profile()

    assert profile == HhProfileData(
        external_id="12345",
        label="Иван Иванов",
        resumes=(
            HhResumeData(hh_id="first-resume", title="Python-разработчик"),
            HhResumeData(hh_id="second-resume", title="Инженер"),
        ),
    )
    assert page.goto_calls == [("https://hh.ru/applicant/resumes", "domcontentloaded")]


def test_profile_rejects_resume_link_from_another_site(tmp_path: Path) -> None:
    page = FakePage()
    page.profile_payload = {
        "externalId": "12345",
        "firstName": "",
        "lastName": "",
        "resumes": [
            {
                "title": "Поддельное резюме",
                "href": "https://example.com/resume/not-hh",
            }
        ],
    }

    with pytest.raises(RuntimeError, match="за пределы"):
        make_browser(page, tmp_path).read_profile()


def test_vacancies_are_read_from_search_page(tmp_path: Path) -> None:
    page = FakePage()
    page.search_payload = {
        "header": "Найдено 1 234 вакансии «Python backend»",
        "vacancies": [
            {
                "title": "Python-разработчик",
                "href": "https://ufa.hh.ru/vacancy/123?query=Python",
                "employer": "Компания",
            },
            {
                "title": "Backend-разработчик",
                "href": "https://hh.ru/vacancy/456",
                "employer": "",
            },
        ],
    }
    browser = make_browser(page, tmp_path)

    result = browser.search_vacancies(
        " Python backend ",
        area="113",
        filters={"order_by": "publication_time", "schedule": ["remote", "fullDay"]},
        page_number=2,
    )

    assert result.found == 1234
    assert [vacancy.hh_id for vacancy in result.vacancies] == ["123", "456"]
    assert result.vacancies[0].source_url == "https://ufa.hh.ru/vacancy/123"
    assert result.vacancies[0].employer_name == "Компания"
    assert result.vacancies[1].employer_name is None
    search_url, wait_until = page.goto_calls[-1]
    assert wait_until == "domcontentloaded"
    assert "text=Python+backend" in search_url
    assert "area=113" in search_url
    assert "page=2" in search_url
    assert "schedule=remote" in search_url
    assert "schedule=fullDay" in search_url


def test_search_rejects_unknown_filter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="не поддерживается"):
        make_browser(FakePage(), tmp_path).search_vacancies(
            "Python",
            filters={"unexpected": "value"},
        )


def test_vacancy_details_are_read_from_page(tmp_path: Path) -> None:
    page = FakePage()
    page.details_payload = {
        "title": "Python-разработчик",
        "employer": "Компания",
        "experience": "1\N{EN DASH}3 года",
        "employment": "Полная занятость",
        "workFormat": "Формат работы: удалённо",
        "description": "Разработка серверной части на Python.",
        "skills": ["Python", "FastAPI", "PostgreSQL"],
        "region": "Москва",
        "address": "ул. Примерная, 1",
        "salary": "от 120 000 до 180 000 ₽ на руки",
        "schedule": "5/2",
        "publishedAt": "2026-07-21T10:30:00+03:00",
        "hasCoverLetter": True,
        "hasScreeningForm": True,
        "hasExternalLink": False,
        "hasTestAssignment": True,
        "availability": "ACTIVE",
    }

    vacancy = make_browser(page, tmp_path).read_vacancy_details(
        "https://ufa.hh.ru/vacancy/123?from=search"
    )

    assert vacancy.hh_id == "123"
    assert vacancy.source_url == "https://ufa.hh.ru/vacancy/123"
    assert vacancy.title == "Python-разработчик"
    assert vacancy.employer_name == "Компания"
    assert vacancy.experience == "1\N{EN DASH}3 года"
    assert vacancy.key_skills == ("Python", "FastAPI", "PostgreSQL")
    assert vacancy.region == "Москва"
    assert vacancy.address == "ул. Примерная, 1"
    assert vacancy.salary_from == Decimal("120000")
    assert vacancy.salary_to == Decimal("180000")
    assert vacancy.salary_currency == "RUR"
    assert vacancy.salary_gross is False
    assert vacancy.has_cover_letter
    assert vacancy.has_screening_form
    assert vacancy.has_test_assignment
    assert vacancy.published_at == datetime(2026, 7, 21, 7, 30, tzinfo=UTC)
    assert vacancy.details_fetched_at is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", (None, None, None, None)),
        (
            "до 200 000 ₽ до вычета налогов",
            (None, Decimal("200000"), "RUR", True),
        ),
        ("от 1 500 $", (Decimal("1500"), None, "USD", None)),
    ],
)
def test_salary_text_is_normalized(
    value: str,
    expected: tuple[Decimal | None, Decimal | None, str | None, bool | None],
) -> None:
    assert VisibleHhBrowser._salary(value) == expected


def test_description_is_split_into_vacancy_sections() -> None:
    responsibilities, required, preferred = VisibleHhBrowser._description_sections(
        "Обязанности:\nРазрабатывать API\nТребования:\nPython\nБудет плюсом:\nDocker"
    )

    assert responsibilities == "Разрабатывать API"
    assert required == "Python"
    assert preferred == "Docker"


def test_resume_details_are_read_without_contacts(tmp_path: Path) -> None:
    page = FakePage()
    page.resume_payload = {
        "title": "Python backend разработчик",
        "experience": "Backend-разработчик на FastAPI",
        "skills": "Python PostgreSQL Docker",
        "education": "Высшее образование",
    }

    details = make_browser(page, tmp_path).read_resume_details("abc123")

    assert details == HhResumeDetails(
        hh_id="abc123",
        title="Python backend разработчик",
        experience="Backend-разработчик на FastAPI",
        skills="Python PostgreSQL Docker",
        education="Высшее образование",
    )


def test_application_with_questions_is_not_submitted(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": ["Укажите Telegram"],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.QUESTIONS_REQUIRED
    assert result.questions == ("Укажите Telegram",)


def test_application_is_submitted_with_cover_letter(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": ["Город не указан"],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    toggle_selector = '[data-qa="vacancy-response-letter-toggle"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator(0)
    page.locators[toggle_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator()
    page.locators["body"] = FakeLocator(text="Отклик отправлен")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_title="Python backend разработчик",
        cover_letter="Содержательное письмо",
    )

    assert result.status is HhApplyStatus.APPLIED
    assert result.warnings == ("Город не указан",)
    assert page.locators[toggle_selector].clicked == 1
    assert page.locators[letter_selector].filled == ["Содержательное письмо"]
    assert page.locators[submit_selector].clicked == 1


def test_application_stops_when_confirmation_cannot_be_read(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()
    page.response.text_error = Error("response body unavailable")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.UNKNOWN_RESULT
    assert result.confirmation == "HTTP 200: "


def test_application_is_verified_in_negotiations(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.response.body = "{}"
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()
    page.locators["body"] = FakeLocator(text="Форма отклика")
    page.locators['a[href*="/vacancy/"]'] = FakeLocator(href="/vacancy/123")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.APPLIED
    assert "подтверждено в списке откликов" in result.confirmation


def test_repeat_application_form_is_not_submitted(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Другое резюме",
        "bodyText": "Форма отклика",
    }
    submit = FakeLocator(text="Откликнуться повторно")
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.ALREADY_APPLIED
    assert submit.clicked == 0


def test_email_and_password_are_filled(tmp_path: Path) -> None:
    page = FakePage()
    prepare_login_page(page)
    browser = make_browser(page, tmp_path)

    status = browser.submit_credentials(HhCredentials(" person@example.com ", "secret"))

    assert status is LoginStatus.MANUAL_ACTION_REQUIRED
    assert page.locators['[data-qa^="account-type-card-APPLICANT"]'].checked
    assert page.locators['[data-qa^="credential-type-EMAIL"]'].checked
    assert page.locators['[data-qa="applicant-login-input-email"]'].filled == ["person@example.com"]
    password_selector = (
        '[data-qa="applicant-login-input-password"], '
        '[data-qa="account-login-password"], input[name="password"]'
    )
    assert page.locators[password_selector].filled == ["secret"]
    assert page.locators['[data-qa="submit-button"]'].clicked == 1


def test_phone_is_normalized_before_filling(tmp_path: Path) -> None:
    page = FakePage()
    page.locators.update(
        {
            '[data-qa^="credential-type-PHONE"]': FakeLocator(checked=False),
            '[data-qa="magritte-phone-input-national-number-input"]': FakeLocator(),
        }
    )
    browser = make_browser(page, tmp_path)

    browser._fill_login(cast(Page, page), "+7 (912) 345-67-89")

    assert page.locators['[data-qa^="credential-type-PHONE"]'].checked
    assert page.locators['[data-qa="magritte-phone-input-national-number-input"]'].filled == [
        "9123456789"
    ]


def test_applicant_form_click_does_not_wait_for_navigation(tmp_path: Path) -> None:
    page = FakePage()
    page.locators.update(
        {
            '[data-qa="applicant-login-card"]': FakeLocator(),
            '[data-qa^="account-type-card-APPLICANT"]': FakeLocator(checked=True),
            '[data-qa="expand-login-by-password"]': FakeLocator(0),
            '[data-qa="submit-button"]': FakeLocator(),
        }
    )

    make_browser(page, tmp_path)._open_applicant_form(cast(Page, page))

    assert page.locators['[data-qa="submit-button"]'].no_wait_after == [True]


@pytest.mark.parametrize(
    ("selector", "status"),
    [
        ('[data-qa*="captcha"], iframe[src*="captcha"]', LoginStatus.CAPTCHA_REQUIRED),
        (
            '[data-qa*="otp"], [data-qa*="verification-code"], input[name*="code"]',
            LoginStatus.CONFIRMATION_REQUIRED,
        ),
        ('[data-qa="form-helper-error"]', LoginStatus.INVALID_CREDENTIALS),
    ],
)
def test_visible_page_states_are_classified(
    tmp_path: Path, selector: str, status: LoginStatus
) -> None:
    page = FakePage()
    page.locators[selector] = FakeLocator(visible=True)

    assert make_browser(page, tmp_path)._classify(cast(Page, page)) is status


def test_authenticated_page_has_priority_over_form_states(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators['[data-qa="form-helper-error"]'] = FakeLocator(visible=True)

    assert make_browser(page, tmp_path)._classify(cast(Page, page)) is LoginStatus.AUTHENTICATED


def test_missing_password_field_returns_current_state(tmp_path: Path) -> None:
    page = FakePage()
    prepare_login_page(page, password_error=True)
    page.locators['[data-qa="form-helper-error"]'] = FakeLocator(visible=True)

    status = make_browser(page, tmp_path).submit_credentials(
        HhCredentials("person@example.com", "secret")
    )

    assert status is LoginStatus.INVALID_CREDENTIALS


def test_browser_must_be_started(tmp_path: Path) -> None:
    browser = VisibleHhBrowser(
        tmp_path,
        "https://hh.ru/account/login",
        "https://hh.ru/applicant/resumes",
        "https://hh.ru/search/vacancy",
        5_000,
    )

    with pytest.raises(RuntimeError, match="не запущен"):
        browser.open_login()


def test_click_requires_exactly_one_element() -> None:
    with pytest.raises(RuntimeError, match="найдено: 2"):
        VisibleHhBrowser._click_unique(cast(Locator, FakeLocator(2)))


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext, *, fail: bool = False) -> None:
        self.context = context
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def launch_persistent_context(self, profile: str, **kwargs: object) -> FakeContext:
        self.calls.append({"profile": profile, **kwargs})
        if self.fail:
            raise RuntimeError("cannot start")
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def start(self) -> FakePlaywright:
        return self.playwright


def test_context_starts_visible_persistent_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = FakePage()
    context = FakeContext(page)
    chromium = FakeChromium(context)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(browser_module, "sync_playwright", lambda: FakeStarter(playwright))
    browser = VisibleHhBrowser(
        tmp_path / "profile",
        "https://hh.ru/account/login",
        "https://hh.ru/applicant/resumes",
        "https://hh.ru/search/vacancy",
        4_000,
    )

    with browser:
        assert page.timeout == 4_000
        assert page.navigation_timeout == 4_000
        assert chromium.calls[0]["headless"] is False
        assert chromium.calls[0]["no_viewport"] is True

    assert (tmp_path / "profile").is_dir()
    assert context.closed
    assert playwright.stopped


def test_failed_browser_start_stops_playwright(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    playwright = FakePlaywright(FakeChromium(FakeContext(FakePage()), fail=True))
    monkeypatch.setattr(browser_module, "sync_playwright", lambda: FakeStarter(playwright))

    with pytest.raises(RuntimeError, match="cannot start"):
        VisibleHhBrowser(
            tmp_path,
            "https://hh.ru/account/login",
            "https://hh.ru/applicant/resumes",
            "https://hh.ru/search/vacancy",
            4_000,
        ).__enter__()

    assert playwright.stopped
