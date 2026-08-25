from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.request import Request

import pytest
from playwright.sync_api import Error, Frame, Locator, Page, Response, TimeoutError

from hugin.adapters import hh_browser as browser_module
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.domain.communications import MessageSendOutcome
from hugin.domain.content import MessageDirection
from hugin.domain.hh import (
    HhApplyStatus,
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
    HhNegotiationData,
    HhNegotiationStatus,
    HhSyncBlockedError,
    HhSyncRetryableError,
)
from hugin.domain.vacancies import VacancyAvailability, VacancyUnavailableError
from hugin.services.hh_login import HhCredentials, LoginStatus

TEST_RESUME_HH_ID = "resume-hash"
RESUME_OPTIONS_SELECTOR = '[data-qa="bottom-sheet-content"]:visible input[name="resumeId"]'
RESUME_DROPDOWN_OPTIONS_SELECTOR = '[data-qa="drop-base"]:visible [role="option"]'
RESUME_GLOBAL_OPTIONS_SELECTOR = '[role="option"][data-qa^="magritte-select-option-"]'


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
        value: str | None = None,
        qa: str | None = None,
        items: list[FakeLocator] | None = None,
        on_click: Callable[[], None] | None = None,
        on_fill: Callable[[str], None] | None = None,
        click_error: bool = False,
    ) -> None:
        self._count = count
        self._items = items
        self.checked = checked
        self.visible = visible
        self.wait_error = wait_error
        self.enabled = enabled
        self.text = text
        self.href = href
        self.value = value
        self.qa = qa
        self.on_click = on_click
        self.on_fill = on_fill
        self.click_error = click_error
        self.clicked = 0
        self.no_wait_after: list[bool] = []
        self.force_clicks: list[bool] = []
        self.trial_clicks: list[bool] = []
        self.filled: list[str] = []

    def count(self) -> int:
        return len(self._items) if self._items is not None else self._count

    def is_checked(self) -> bool:
        return self.checked

    def check(self, *, force: bool = False) -> None:
        assert force
        self.checked = True

    def click(
        self,
        *,
        force: bool = False,
        no_wait_after: bool = False,
        timeout: int | None = None,
        trial: bool = False,
    ) -> None:
        assert timeout is None or timeout > 0
        self.force_clicks.append(force)
        self.trial_clicks.append(trial)
        if self.click_error:
            raise TimeoutError("click")
        if trial:
            return
        self.clicked += 1
        self.no_wait_after.append(no_wait_after)
        if force:
            self.checked = True
        if self.on_click is not None:
            self.on_click()

    def fill(self, value: str) -> None:
        self.filled.append(value)
        self.value = value
        if self.on_fill is not None:
            self.on_fill(value)

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state in {"attached", "visible"}
        assert timeout > 0
        if self.wait_error:
            raise TimeoutError("wait")

    def all(self) -> list[FakeLocator]:
        if self._items is not None:
            return self._items
        return [self] if self._count else []

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        if name == "href":
            return self.href
        if name == "value":
            return self.value
        if name == "data-qa":
            return self.qa
        return None

    @property
    def first(self) -> FakeLocator:
        if self._items is not None:
            return self._items[0]
        return self


class FakeKeyboard:
    def __init__(self, on_press: Callable[[str], None] | None = None) -> None:
        self.on_press = on_press
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)
        if self.on_press is not None:
            self.on_press(key)


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
        self.application_payloads: list[object] | None = None
        self.application_payload_index = 0
        self.fill_payload: object = None
        self.fill_result: object = {"filled": [], "skipped": []}
        self.verify_payload: object = None
        self.verify_result: object = {"missingRequired": [], "mismatched": []}
        self.negotiations_payload: object = []
        self.opened_chat = True
        self.opened_vacancy_ids: list[str] = []
        self.frames: list[Frame] = []
        self.keyboard = FakeKeyboard()
        self.response = FakeResponse()
        self.goto_response: FakeResponse | None = None
        self.goto_final_url: str | None = None
        self.goto_error: Error | None = None
        self.load_state_error: Error | None = None
        self.load_state_callback: Callable[[], None] | None = None
        self.load_state_calls: list[tuple[str, int]] = []
        self.window_probe_error: Error | None = None
        self.closed = False
        self.route_handler: Callable[[object, object], None] | None = None
        self.last_route: FakeRoute | None = None
        self.locators['[data-qa="resume-title"]'] = FakeLocator()
        self.locators['[data-qa="vacancy-response-link-top"]:visible'] = FakeLocator()
        if "/applicant/" in url:
            self.locators[browser_module._AUTHENTICATED_APPLICANT_SELECTOR] = FakeLocator(
                visible=True
            )
        selected_resume = FakeLocator(
            value=TEST_RESUME_HH_ID,
            checked=True,
            on_click=lambda: self.locators.__setitem__(
                RESUME_OPTIONS_SELECTOR,
                FakeLocator(items=[]),
            ),
        )
        self.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[selected_resume])

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator(0))

    def goto(self, url: str, *, wait_until: str) -> FakeResponse | None:
        self.goto_calls.append((url, wait_until))
        if self.goto_error is not None:
            raise self.goto_error
        self.url = self.goto_final_url or url
        return self.goto_response

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.load_state_calls.append((state, timeout))
        if self.load_state_callback is not None:
            self.load_state_callback()
        if self.load_state_error is not None:
            raise self.load_state_error

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_timeout(self, timeout: int) -> None:
        assert timeout in {250, 500, 1_000, 1_500, 3_000}

    def evaluate(self, expression: str, argument: object = None) -> object:
        if expression == browser_module._WINDOW_LIVENESS_SCRIPT:
            if self.window_probe_error is not None:
                raise self.window_probe_error
            return True
        if expression == browser_module.FILL_APPLICATION_FORM_SCRIPT:
            self.fill_payload = argument
            return self.fill_result
        if expression == browser_module.VERIFY_APPLICATION_FORM_SCRIPT:
            self.verify_payload = argument
            return self.verify_result
        if expression == browser_module.NEGOTIATIONS_SCRIPT:
            return self.negotiations_payload
        if expression == browser_module.OPEN_NEGOTIATION_CHAT_SCRIPT:
            assert isinstance(argument, str)
            self.opened_vacancy_ids.append(argument)
            return self.opened_chat
        if expression == browser_module.RESUME_DETAILS_SCRIPT:
            return self.resume_payload
        if "ResumeProfileFront-InitialState" in expression:
            return self.profile_payload
        if "vacancy-serp__vacancy" in expression:
            return self.search_payload
        if "vacancy-description" in expression:
            return self.details_payload
        if "task-question" in expression:
            payload = self.application_payload
            if self.application_payloads:
                index = min(
                    self.application_payload_index,
                    len(self.application_payloads) - 1,
                )
                self.application_payload_index += 1
                payload = self.application_payloads[index]
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.setdefault(
                    "vacancyId",
                    VisibleHhBrowser._application_url_vacancy_id(self.url),
                )
                payload.setdefault("resumeHhId", TEST_RESUME_HH_ID)
                letter = self.locators.get(
                    '[data-qa="vacancy-response-popup-form-letter-input"]'
                )
                payload.setdefault(
                    "coverLetter",
                    letter.value if letter is not None and letter.value is not None else "",
                )
            return payload
        raise AssertionError("unexpected browser script")

    def expect_response(self, predicate: object, *, timeout: int) -> FakeResponseInfo:
        assert timeout > 0
        assert callable(predicate)
        route = FakeRoute()
        self.last_route = route
        self.response.request.url = self.response.url
        if self.route_handler is not None:
            self.route_handler(route, self.response.request)
        return FakeResponseInfo(
            self.response,
            matches=not route.aborted and bool(predicate(self.response)),
        )

    def route(self, pattern: str, handler: Callable[[object, object], None]) -> None:
        assert "vacancy_response" in pattern
        self.route_handler = handler

    def unroute(self, pattern: str, handler: Callable[[object, object], None]) -> None:
        assert "vacancy_response" in pattern
        if self.route_handler is handler:
            self.route_handler = None


class FakeFrame:
    def __init__(
        self,
        *,
        url: str = "https://chatik.hh.ru/chat/101",
        messages_payload: object = None,
        messages_payloads: list[object] | None = None,
    ) -> None:
        self.url = url
        self.locators: dict[str, FakeLocator] = {}
        self.messages_payload = [] if messages_payload is None else messages_payload
        self.messages_payloads = messages_payloads
        self.messages_payload_index = 0
        self.evaluated_vacancy_ids: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator(0))

    def evaluate(self, expression: str, argument: object = None) -> object:
        if expression != browser_module.CHAT_MESSAGES_SCRIPT:
            raise AssertionError("unexpected frame script")
        assert isinstance(argument, str)
        self.evaluated_vacancy_ids.append(argument)
        if self.messages_payloads:
            index = min(self.messages_payload_index, len(self.messages_payloads) - 1)
            self.messages_payload_index += 1
            return self.messages_payloads[index]
        return self.messages_payload


class FakeRequest:
    method = "POST"

    def __init__(self) -> None:
        self.url = "https://hh.ru/applicant/vacancy_response?vacancyId=123"
        self.post_data: str | None = f"resumeHash={TEST_RESUME_HH_ID}"


class FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    def abort(self, error_code: str = "failed") -> None:
        assert error_code
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class FakeResponse:
    def __init__(self) -> None:
        self.request = FakeRequest()
        self.url = "https://hh.ru/applicant/vacancy_response?vacancyId=123"
        self.status = 200
        self.headers: dict[str, str] = {}
        self.text_error: Error | None = None
        self.body = '{"success":true}'

    def text(self) -> str:
        if self.text_error is not None:
            raise self.text_error
        return self.body

    def header_value(self, name: str) -> str | None:
        return self.headers.get(name.casefold())


class FakeResponseInfo:
    def __init__(self, response: FakeResponse, *, matches: bool = True) -> None:
        self.value = response
        self.matches = matches

    def __enter__(self) -> FakeResponseInfo:
        return self

    def __exit__(self, *args: object) -> None:
        if not self.matches:
            raise TimeoutError("response")
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
    prepare_login_page(page)
    browser = make_browser(page, tmp_path)

    browser.open_login()

    assert page.goto_calls == [("https://hh.ru/account/login?role=applicant", "commit")]
    assert page.load_state_calls == [("domcontentloaded", 5_000)]
    assert not browser.is_authenticated()
    page.url = "https://hh.ru/applicant/resumes"
    assert browser.is_authenticated()
    page.url = "https://ufa.hh.ru/applicant/resumes"
    assert browser.is_authenticated()
    page.url = "https://not-hh.ru/applicant/resumes"
    assert not browser.is_authenticated()


def test_public_hh_page_requires_an_authenticated_account_marker(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/vacancy/123")
    browser = make_browser(page, tmp_path)

    assert not browser.is_authenticated()

    page.locators[browser_module._AUTHENTICATED_APPLICANT_SELECTOR] = FakeLocator(
        visible=True
    )

    assert browser.is_authenticated()


def test_minimized_authenticated_page_uses_present_account_marker(
    tmp_path: Path,
) -> None:
    page = FakePage("https://baymak.hh.ru/")
    page.locators[browser_module._AUTHENTICATED_APPLICANT_SELECTOR] = FakeLocator(
        visible=False
    )

    assert make_browser(page, tmp_path).is_authenticated()


@pytest.mark.parametrize(
    "selector",
    [browser_module._CAPTCHA_SELECTOR, browser_module._CONFIRMATION_CODE_SELECTOR],
)
def test_challenge_is_never_treated_as_authenticated(
    tmp_path: Path,
    selector: str,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators[selector] = FakeLocator(visible=False)

    assert not make_browser(page, tmp_path).is_authenticated()


def test_account_warning_is_never_treated_as_authenticated(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators["body"] = FakeLocator(
        text="Подозрительная активность. Подтвердите аккаунт.",
    )
    browser = make_browser(page, tmp_path)

    assert browser.has_account_warning()
    assert not browser.is_authenticated()
    assert browser.authentication_status() is LoginStatus.ACCOUNT_WARNING


def test_browser_reports_whether_review_window_is_open(tmp_path: Path) -> None:
    page = FakePage()
    browser = make_browser(page, tmp_path)

    assert browser.is_open()
    page.closed = True
    assert not browser.is_open()


def test_browser_detects_closed_window_before_page_flag_updates(tmp_path: Path) -> None:
    page = FakePage()
    browser = make_browser(page, tmp_path)
    page.window_probe_error = Error("Target page, context or browser has been closed")

    assert not page.is_closed()
    assert not browser.is_open()


def test_aborted_login_redirect_is_accepted_for_authenticated_page(tmp_path: Path) -> None:
    page = FakePage("https://ufa.hh.ru/applicant/resumes")
    page.goto_error = Error("net::ERR_ABORTED")
    browser = make_browser(page, tmp_path)

    browser.open_login()

    assert browser.is_authenticated()


def test_login_waits_for_authenticated_redirect_before_credentials(
    tmp_path: Path,
) -> None:
    page = FakePage()

    def finish_redirect() -> None:
        page.url = "https://baymak.hh.ru/"
        page.locators[browser_module._AUTHENTICATED_APPLICANT_SELECTOR] = FakeLocator(
            visible=True
        )

    page.load_state_callback = finish_redirect
    browser = make_browser(page, tmp_path)

    browser.open_login()
    status = browser.submit_credentials(HhCredentials("+7 912 345-67-89", "secret"))

    assert status is LoginStatus.AUTHENTICATED
    assert (
        page.locators.get('[data-qa="magritte-phone-input-national-number-input"]') is None
    )


def test_login_without_ready_page_retries_before_loading_credentials(
    tmp_path: Path,
) -> None:
    page = FakePage()

    with pytest.raises(HhSyncRetryableError) as error:
        make_browser(page, tmp_path).open_login()

    assert error.value.code == "HH_LOGIN_FORM_TIMEOUT"
    assert error.value.retry_after_seconds == browser_module._NETWORK_RETRY_SECONDS


@pytest.mark.parametrize(
    "navigation_error",
    (
        Error("Page.goto: net::ERR_TIMED_OUT at https://hh.ru/account/login"),
        TimeoutError("Page.goto: Timeout 60000ms exceeded"),
    ),
)
def test_login_navigation_timeout_is_retried_automatically(
    tmp_path: Path,
    navigation_error: Error,
) -> None:
    page = FakePage()
    page.goto_error = navigation_error

    with pytest.raises(HhSyncRetryableError) as error:
        make_browser(page, tmp_path).open_login()

    assert error.value.code == "HH_NETWORK_TIMEOUT"
    assert error.value.retry_after_seconds == browser_module._NETWORK_RETRY_SECONDS
    assert "повторена автоматически" in str(error.value)


def test_login_rate_limit_preserves_platform_retry_after(tmp_path: Path) -> None:
    page = FakePage()
    response = FakeResponse()
    response.status = 429
    response.headers["retry-after"] = "172800"
    page.goto_response = response

    with pytest.raises(HhSyncRetryableError) as error:
        make_browser(page, tmp_path).open_login()

    assert error.value.code == "HH_RATE_LIMITED"
    assert error.value.retry_after_seconds == 172_800


def test_login_forbidden_response_requires_manual_account_review(tmp_path: Path) -> None:
    page = FakePage()
    response = FakeResponse()
    response.status = 403
    page.goto_response = response

    with pytest.raises(HhSyncBlockedError) as error:
        make_browser(page, tmp_path).open_login()

    assert error.value.code == "ACCOUNT_WARNING"


@pytest.mark.parametrize(
    "url",
    (
        "http://hh.ru/account/login?role=applicant",
        "https://example.org/account/login?role=applicant",
    ),
)
def test_credentials_are_never_filled_outside_secure_hh(
    tmp_path: Path,
    url: str,
) -> None:
    page = FakePage(url)
    prepare_login_page(page)
    password = page.locators[
        (
            '[data-qa="applicant-login-input-password"], '
            '[data-qa="account-login-password"], input[name="password"]'
        )
    ]

    with pytest.raises(HhSyncBlockedError) as error:
        make_browser(page, tmp_path).submit_credentials(
            HhCredentials("candidate@example.org", "secret")
        )

    assert error.value.code == "UNSAFE_LOGIN_TARGET"
    assert password.filled == []


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
                "publishedAt": "2026-07-28T17:29:53.587+03:00",
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
    assert result.vacancies[0].published_at == datetime(
        2026,
        7,
        28,
        14,
        29,
        53,
        587000,
        tzinfo=UTC,
    )
    assert result.vacancies[1].employer_name is None
    assert result.vacancies[1].published_at is None
    search_url, wait_until = page.goto_calls[-1]
    assert wait_until == "domcontentloaded"
    assert "text=Python+backend" in search_url
    assert "area=113" in search_url
    assert "page=2" in search_url
    assert "schedule=remote" in search_url
    assert "schedule=fullDay" in search_url


def test_search_reads_publication_time_from_current_hh_card_data() -> None:
    assert "key.startsWith('__reactFiber$')" in browser_module.VACANCY_SEARCH_SCRIPT
    assert "String(candidate.vacancyId || '') !== vacancyId" in (
        browser_module.VACANCY_SEARCH_SCRIPT
    )
    assert "publicationTime.$.trim()" in browser_module.VACANCY_SEARCH_SCRIPT
    assert "candidate.creationTime" not in browser_module.VACANCY_SEARCH_SCRIPT


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
        "publishedAt": "Вакансия опубликована 21 июля 2026",
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
    assert vacancy.published_at is not None
    assert (
        vacancy.published_at.year,
        vacancy.published_at.month,
        vacancy.published_at.day,
    ) == (2026, 7, 21)
    assert vacancy.details_fetched_at is not None


@pytest.mark.parametrize(
    ("status", "availability"),
    [
        (404, VacancyAvailability.UNAVAILABLE),
        (410, VacancyAvailability.UNAVAILABLE),
        (200, VacancyAvailability.ARCHIVED),
        (200, VacancyAvailability.CLOSED),
    ],
)
def test_unavailable_vacancy_details_are_reported_before_title_wait(
    tmp_path: Path,
    status: int,
    availability: VacancyAvailability,
) -> None:
    page = FakePage()
    response = FakeResponse()
    response.status = status
    page.goto_response = response
    page.details_payload = {
        "availability": (availability.value if status == 200 else VacancyAvailability.ACTIVE.value)
    }

    with pytest.raises(VacancyUnavailableError) as error:
        make_browser(page, tmp_path).read_vacancy_details("https://hh.ru/vacancy/123")

    assert error.value.availability is availability


def test_restricted_vacancy_page_is_reported_without_waiting_for_title(
    tmp_path: Path,
) -> None:
    page = FakePage()
    page.details_payload = {"availability": VacancyAvailability.ACTIVE.value}
    page.locators["body"] = FakeLocator(
        text=(
            "Вам недоступна эта вакансия. Войдите как пользователь, "
            "у которого есть доступ на просмотр."
        )
    )

    with pytest.raises(VacancyUnavailableError) as error:
        make_browser(page, tmp_path).read_vacancy_details("https://uchaly.hh.ru/vacancy/134858747")

    assert error.value.availability is VacancyAvailability.UNAVAILABLE
    assert "недоступна эта вакансия" in browser_module.VACANCY_DETAILS_SCRIPT


def test_vacancy_details_script_recognizes_plain_external_form_urls() -> None:
    assert r"forms\.gle" in browser_module.VACANCY_DETAILS_SCRIPT
    assert "externalApplicationText" in browser_module.VACANCY_DETAILS_SCRIPT


def test_application_snapshot_ignores_unrelated_hashes_in_react_state() -> None:
    script = browser_module.APPLICATION_FORM_SCRIPT

    assert "const resumeHash = (value)" in script
    assert "'shortExperience' in value" in script
    assert "normalizedKey === 'hash'" not in script


def test_visible_russian_publication_date_is_parsed() -> None:
    parsed = VisibleHhBrowser._date_time("Вакансия опубликована 30 июля 2026")

    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 7, 30)


def test_structured_job_posting_date_is_parsed_with_timezone() -> None:
    parsed = VisibleHhBrowser._date_time("2026-07-30T19:17:03.720+03:00")

    assert parsed == datetime(2026, 7, 30, 16, 17, 3, 720000, tzinfo=UTC)
    assert 'script[type="application/ld+json"]' in browser_module.VACANCY_DETAILS_SCRIPT
    assert "candidate.datePosted || candidate.datePublished" in (
        browser_module.VACANCY_DETAILS_SCRIPT
    )


def test_publication_date_without_year_is_not_in_the_future() -> None:
    parsed = VisibleHhBrowser._date_time("Вакансия опубликована 31 декабря")

    assert parsed is not None
    assert (parsed.month, parsed.day) == (12, 31)
    assert parsed <= datetime.now(UTC) + timedelta(days=1)


@pytest.mark.parametrize("status", [404, 410])
def test_apply_reports_closed_vacancy_before_looking_for_response_button(
    tmp_path: Path,
    status: int,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    response = FakeResponse()
    response.status = status
    page.goto_response = response
    page.locators["body"] = FakeLocator(text="Вакансия недоступна")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.VACANCY_CLOSED


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
        "city": "Санкт-Петербург",
        "salary": "180 000 ₽ на руки",
        "employment": "полная занятость",
        "workFormat": "удалённо, гибрид",
        "relocation": "Не готов к переезду",
        "businessTrips": "готов к редким командировкам",
        "experience": (
            "PointPulse\nBackend-разработчик на FastAPI\nРедактировать\n\n"
            "Яндекс Крауд\nСпециалист по автоматизации\nРазвернуть"
        ),
        "experienceBlocks": [
            {
                "company": "PointPulse",
                "position": "Backend-разработчик",
                "period": "Январь 2026 — настоящее время",
                "description": "Разрабатываю сервис на FastAPI.\nРазвернуть",
                "text": (
                    "PointPulse\nBackend-разработчик\n"
                    "Январь 2026 — настоящее время\nРазрабатываю сервис на FastAPI.\n"
                    "Редактировать"
                ),
            },
            {
                "company": "Яндекс Крауд",
                "position": "Специалист по автоматизации",
                "period": "Август 2025 — декабрь 2025",
                "description": "Создавал backend-прототипы.\nРазвернуть",
                "text": (
                    "Яндекс Крауд\nСпециалист по автоматизации\n"
                    "Август 2025 — декабрь 2025\nСоздавал backend-прототипы.\n"
                    "Редактировать"
                ),
            },
        ],
        "skills": "Python PostgreSQL Docker\nУказать уровни\nДобавить\nРедактировать",
        "education": "Высшее образование\nРедактировать",
        "about": "Python backend-разработчик.\nРазвернуть\nРедактировать",
    }

    details = make_browser(page, tmp_path).read_resume_details("abc123")

    assert details == HhResumeDetails(
        hh_id="abc123",
        title="Python backend разработчик",
        experience=(
            "PointPulse\nBackend-разработчик на FastAPI\n\n"
            "Яндекс Крауд\nСпециалист по автоматизации"
        ),
        skills="Python PostgreSQL Docker",
        education="Высшее образование",
        city="Санкт-Петербург",
        salary="180 000 ₽ на руки",
        employment="полная занятость",
        work_format="удалённо, гибрид",
        relocation="Не готов к переезду",
        business_trips="готов к редким командировкам",
        about="Python backend-разработчик.",
        experience_blocks=(
            HhResumeExperienceBlock(
                company="PointPulse",
                position="Backend-разработчик",
                period="Январь 2026 — настоящее время",
                description="Разрабатываю сервис на FastAPI.",
                text=(
                    "PointPulse\nBackend-разработчик\n"
                    "Январь 2026 — настоящее время\nРазрабатываю сервис на FastAPI."
                ),
            ),
            HhResumeExperienceBlock(
                company="Яндекс Крауд",
                position="Специалист по автоматизации",
                period="Август 2025 — декабрь 2025",
                description="Создавал backend-прототипы.",
                text=(
                    "Яндекс Крауд\nСпециалист по автоматизации\n"
                    "Август 2025 — декабрь 2025\nСоздавал backend-прототипы."
                ),
            ),
        ),
    )


def test_resume_details_script_uses_complete_structured_fields() -> None:
    assert '[data-qa="profile-experience-company-card"]' in browser_module.RESUME_DETAILS_SCRIPT
    assert "resumeState.experience" in browser_module.RESUME_DETAILS_SCRIPT
    assert "stateValues('employment')" in browser_module.RESUME_DETAILS_SCRIPT
    assert "stateValues('workFormats')" in browser_module.RESUME_DETAILS_SCRIPT
    assert "https://api.hh.ru/areas/" in browser_module.RESUME_DETAILS_SCRIPT
    assert "fragment(/переезд/i)" in browser_module.RESUME_DETAILS_SCRIPT
    assert "fragment(/командиров/i)" in browser_module.RESUME_DETAILS_SCRIPT
    assert "|| mobility" not in browser_module.RESUME_DETAILS_SCRIPT


def test_application_statuses_are_read_from_negotiations(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyId": "101",
            "vacancyHref": "/vacancy/101",
            "statusQa": "negotiations-tag negotiations-item-not-viewed",
            "statusLabel": "Не просмотрен",
            "chatAvailable": True,
        },
        {
            "vacancyHref": "https://hh.ru/vacancy/202",
            "statusQa": "negotiations-tag negotiations-item-discard",
            "statusLabel": "Отказ",
            "chatAvailable": False,
        },
        {
            "vacancyHref": "https://example.com/vacancy/303",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": False,
        },
    ]

    statuses = make_browser(page, tmp_path).read_application_statuses()

    assert statuses == (
        HhNegotiationData(
            vacancy_id="101",
            status=HhNegotiationStatus.APPLIED,
            status_label="Не просмотрен",
            chat_available=True,
        ),
        HhNegotiationData(
            vacancy_id="202",
            status=HhNegotiationStatus.REJECTED,
            status_label="Отказ",
        ),
    )
    assert page.goto_calls[-1][0] == "https://hh.ru/applicant/negotiations"
    assert "__reactFiber$" in browser_module.NEGOTIATIONS_SCRIPT


def test_application_statuses_open_current_negotiation_cards(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "",
            "statusQa": "negotiations-tag negotiations-item-not-viewed",
            "statusLabel": "Не просмотрен",
            "chatAvailable": True,
        },
        {
            "vacancyHref": "",
            "statusQa": "negotiations-tag negotiations-item-discard",
            "statusLabel": "Отказ",
            "chatAvailable": False,
        },
    ]
    first = FakeLocator(on_click=lambda: setattr(page, "url", "https://hh.ru/vacancy/101"))
    second = FakeLocator(on_click=lambda: setattr(page, "url", "https://hh.ru/vacancy/202"))
    page.locators['[data-qa="negotiations-item"]'] = FakeLocator(items=[first, second])

    statuses = make_browser(page, tmp_path).read_application_statuses()

    assert [status.vacancy_id for status in statuses] == ["101", "202"]
    assert statuses[0].status is HhNegotiationStatus.APPLIED
    assert statuses[1].status is HhNegotiationStatus.REJECTED
    assert first.clicked == 1
    assert second.clicked == 1


def test_application_statuses_stop_if_current_list_changes(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "",
            "statusQa": "negotiations-tag negotiations-item-not-viewed",
            "statusLabel": "Не просмотрен",
            "chatAvailable": True,
        }
    ]
    page.locators['[data-qa="negotiations-item"]'] = FakeLocator(items=[])

    with pytest.raises(RuntimeError, match="изменился"):
        make_browser(page, tmp_path).read_application_statuses()


def test_recruiter_messages_are_read_from_tracked_chats(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "/vacancy/101",
            "statusQa": "negotiations-tag negotiations-item-viewed",
            "statusLabel": "Просмотрен",
            "chatAvailable": True,
        },
        {
            "vacancyHref": "/vacancy/999",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        },
    ]
    frame = FakeFrame(
        messages_payload=[
            {
                "vacancyId": "101",
                "messageId": "message-1",
                "direction": "INCOMING",
                "body": "Приглашаем на собеседование.",
                "displayedTime": "10:15",
            },
            {
                "vacancyId": "101",
                "messageId": "message-2",
                "direction": "OUTGOING",
                "body": "Спасибо!",
                "displayedTime": "10:20",
            },
        ]
    )
    page.frames = [cast(Frame, frame)]
    page.locators['[data-qa="chatik-close-chatik"]'] = FakeLocator()

    messages = make_browser(page, tmp_path).read_recruiter_messages(("101",))

    assert [(item.hh_id, item.direction, item.body) for item in messages] == [
        ("message-1", MessageDirection.INCOMING, "Приглашаем на собеседование."),
        ("message-2", MessageDirection.OUTGOING, "Спасибо!"),
    ]
    assert page.opened_vacancy_ids == ["101"]
    assert frame.evaluated_vacancy_ids == ["101"]


def test_recruiter_messages_open_negotiations_once_for_all_chats(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "/vacancy/101",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        },
        {
            "vacancyHref": "/vacancy/202",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        },
    ]
    frame = FakeFrame(
        messages_payloads=[
            [
                {
                    "vacancyId": "101",
                    "messageId": "message-101",
                    "direction": "INCOMING",
                    "body": "Первое сообщение",
                }
            ],
            [
                {
                    "vacancyId": "202",
                    "messageId": "message-202",
                    "direction": "INCOMING",
                    "body": "Второе сообщение",
                }
            ],
        ]
    )
    page.frames = [cast(Frame, frame)]
    close = FakeLocator()
    page.locators['[data-qa="chatik-close-chatik"]'] = close

    messages = make_browser(page, tmp_path).read_recruiter_messages(("101", "202"))

    assert [message.vacancy_id for message in messages] == ["101", "202"]
    assert page.goto_calls == [
        ("https://hh.ru/applicant/negotiations", "domcontentloaded")
    ]
    assert page.opened_vacancy_ids == ["101", "202"]
    assert close.clicked == 2


def test_recruiter_messages_are_read_from_later_negotiations_page(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    first_page = FakeLocator(text="1")

    def open_second_page() -> None:
        page.negotiations_payload = [
            {
                "vacancyHref": "/vacancy/202",
                "statusQa": "",
                "statusLabel": "",
                "chatAvailable": True,
            }
        ]

    second_page = FakeLocator(text="2", on_click=open_second_page)
    page.locators['[data-qa^="number-pages-"]'] = FakeLocator(
        items=[first_page, second_page]
    )
    page.locators['[data-qa*="number-pages-selected"]'] = first_page
    page.locators['[data-qa^="number-pages-1"]'] = first_page
    page.locators['[data-qa^="number-pages-2"]'] = second_page
    frame = FakeFrame(
        messages_payload=[
            {
                "vacancyId": "202",
                "messageId": "message-202",
                "direction": "INCOMING",
                "body": "Сообщение со второй страницы",
            }
        ]
    )
    page.frames = [cast(Frame, frame)]
    page.locators['[data-qa="chatik-close-chatik"]'] = FakeLocator()

    messages = make_browser(page, tmp_path).read_recruiter_messages(("202",))

    assert [message.hh_id for message in messages] == ["message-202"]
    assert second_page.clicked == 1


def test_recruiter_message_read_fails_if_advertised_chat_does_not_open(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "/vacancy/101",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        }
    ]
    page.opened_chat = False

    with pytest.raises(RuntimeError, match="не открыл"):
        make_browser(page, tmp_path).read_recruiter_messages(("101",))


def test_recruiter_message_read_fails_if_chat_frame_does_not_load(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "/vacancy/101",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        }
    ]

    with pytest.raises(RuntimeError, match="не загрузилась"):
        make_browser(page, tmp_path).read_recruiter_messages(("101",))


def test_recruiter_message_read_does_not_accept_silently_empty_chat(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "/vacancy/101",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        }
    ]
    page.frames = [cast(Frame, FakeFrame(messages_payload=[]))]

    with pytest.raises(RuntimeError, match="ни одного сообщения"):
        make_browser(page, tmp_path).read_recruiter_messages(("101",))


def test_recruiter_message_read_rejects_messages_from_another_chat(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.negotiations_payload = [
        {
            "vacancyHref": "/vacancy/101",
            "statusQa": "",
            "statusLabel": "",
            "chatAvailable": True,
        }
    ]
    page.frames = [
        cast(
            Frame,
            FakeFrame(
                messages_payload=[
                    {
                        "vacancyId": "999",
                        "messageId": "message-1",
                        "direction": "INCOMING",
                        "body": "Сообщение из другой переписки",
                    }
                ]
            ),
        )
    ]

    with pytest.raises(RuntimeError, match="другой переписки"):
        make_browser(page, tmp_path).read_recruiter_messages(("101",))


@pytest.mark.parametrize(
    ("body_text", "expected_code"),
    [
        ("Подозрительная активность. Подтвердите аккаунт.", "ACCOUNT_WARNING"),
        ("Аккаунт заблокирован", "ACCOUNT_WARNING"),
    ],
)
def test_status_check_reports_account_warning(
    tmp_path: Path,
    body_text: str,
    expected_code: str,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators["body"] = FakeLocator(text=body_text)

    with pytest.raises(HhSyncBlockedError) as error:
        make_browser(page, tmp_path).read_application_statuses()

    assert error.value.code == expected_code


def test_status_check_retries_temporary_text_limit(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators["body"] = FakeLocator(text="Слишком много запросов. Попробуйте позже.")

    with pytest.raises(HhSyncRetryableError) as error:
        make_browser(page, tmp_path).read_application_statuses()

    assert error.value.code == "HH_RATE_LIMITED"
    assert error.value.retry_after_seconds == browser_module._TEMPORARY_REQUEST_RETRY_SECONDS


def test_status_check_respects_retry_after_header(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    response = FakeResponse()
    response.status = 429
    response.headers["retry-after"] = "180"
    page.goto_response = response

    with pytest.raises(HhSyncRetryableError) as error:
        make_browser(page, tmp_path).read_application_statuses()

    assert error.value.code == "HH_RATE_LIMITED"
    assert error.value.retry_after_seconds == 180


def test_status_check_http_403_requires_manual_account_review(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    response = FakeResponse()
    response.status = 403
    page.goto_response = response

    with pytest.raises(HhSyncBlockedError) as error:
        make_browser(page, tmp_path).read_application_statuses()

    assert error.value.code == "ACCOUNT_WARNING"
    assert "ручная проверка" in str(error.value)


def test_application_with_questions_is_not_submitted(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": ["Укажите Telegram"],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://uchaly.hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.QUESTIONS_REQUIRED
    assert result.questions == ("Укажите Telegram",)
    assert page.goto_calls[0][0] == "https://hh.ru/vacancy/123"


def test_empty_letter_preflight_accepts_disabled_submit_but_real_submit_does_not(
    tmp_path: Path,
) -> None:
    payload = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    preflight_page = FakePage("https://hh.ru/applicant/resumes")
    preflight_page.application_payload = payload
    preflight_submit = FakeLocator(enabled=False)
    preflight_page.locators[submit_selector] = preflight_submit

    preflight = make_browser(preflight_page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="",
        submit=False,
    )

    assert preflight.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert preflight_submit.clicked == 0

    submit_page = FakePage("https://hh.ru/applicant/resumes")
    submit_page.application_payload = payload
    real_submit = FakeLocator(enabled=False)
    submit_page.locators[submit_selector] = real_submit

    applied = make_browser(submit_page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="",
        submit=True,
        submit_guard=lambda: True,
    )

    assert applied.status is HhApplyStatus.RETRYABLE_ERROR
    assert real_submit.clicked == 0


def test_application_selects_exact_resume_before_reading_questions(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    payload: dict[str, object] = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Нефтяной геолог",
        "bodyText": "Форма отклика",
    }
    page.application_payload = payload

    def select_target() -> None:
        payload["resumeTitle"] = "Python backend разработчик"
        payload["fields"] = [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
            }
        ]
        page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])

    other = FakeLocator(value="other-resume")
    target = FakeLocator(value=TEST_RESUME_HH_ID, on_click=select_target)
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[other, target])

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.QUESTIONS_REQUIRED
    assert result.questions == ("Укажите Telegram",)
    assert other.clicked == 0
    assert target.clicked == 1
    assert target.force_clicks == [True]


def test_application_selects_exact_resume_from_desktop_dropdown(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    payload: dict[str, object] = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Нефтяной геолог",
        "bodyText": "Форма отклика",
    }
    page.application_payload = payload

    def select_target() -> None:
        payload["resumeTitle"] = "Python backend разработчик"
        page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[])

    other = FakeLocator(qa="magritte-select-option-other-resume")
    target = FakeLocator(
        qa=f"magritte-select-option-{TEST_RESUME_HH_ID}",
        on_click=select_target,
    )
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[other, target])
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert other.clicked == 0
    assert target.clicked == 1
    assert target.force_clicks == [False]


def test_application_stops_when_resume_dropdown_stays_open(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "resumeHhId": "другое-резюме",
        "bodyText": "Форма отклика",
    }
    target = FakeLocator(qa=f"magritte-select-option-{TEST_RESUME_HH_ID}")
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[target])
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert "не закрылся" in result.confirmation
    assert submit.clicked == 0
    assert page.keyboard.pressed == ["Escape"]


def test_application_closes_selected_resume_dropdown_with_escape(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    payload: dict[str, object] = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Нефтяной геолог",
        "bodyText": "Форма отклика",
    }
    page.application_payload = payload

    def select_target() -> None:
        payload["resumeTitle"] = "Python backend разработчик"

    target = FakeLocator(
        qa=f"magritte-select-option-{TEST_RESUME_HH_ID}",
        on_click=select_target,
    )
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[target])
    page.keyboard.on_press = lambda key: page.locators.__setitem__(
        RESUME_DROPDOWN_OPTIONS_SELECTOR,
        FakeLocator(items=[]),
    )
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert target.clicked == 1
    assert page.keyboard.pressed == ["Escape"]


def test_cover_letter_timeout_before_submit_is_retryable(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    toggle_selector = '[data-qa="add-cover-letter"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator(0)
    page.locators[toggle_selector] = FakeLocator(click_error=True)
    page.locators[submit_selector] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert "до нажатия" in result.confirmation
    assert page.locators[submit_selector].clicked == 0


def test_application_stops_when_exact_resume_is_absent(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Другое резюме",
        "bodyText": "Форма отклика",
    }
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[FakeLocator(value="other-resume")])
    letter = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = letter
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RESUME_MISMATCH
    assert "точным номером" in result.confirmation
    assert letter.filled == []
    assert submit.clicked == 0


def test_screening_form_is_parsed_with_field_constraints(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": 100,
                "formatHint": "@username",
                "hasAttachment": False,
                "hasExternalAction": False,
                "hasTestAssignment": False,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.QUESTIONS_REQUIRED
    assert result.screening_form == HhScreeningForm(
        fields=(
            HhScreeningField(
                key="name:telegram",
                question="Укажите Telegram",
                field_type="text",
                is_required=True,
                max_length=100,
                format_hint="@username",
            ),
        )
    )


def test_unknown_question_requirement_is_treated_as_required(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "fields": [
            {
                "key": "name:unknown",
                "question": "Расскажите о себе",
                "fieldType": "textarea",
                "options": [],
                "maxLength": None,
            },
            {
                "key": "name:optional",
                "question": "Дополнительный комментарий",
                "fieldType": "textarea",
                "isRequired": False,
                "options": [],
                "maxLength": None,
            },
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }

    snapshot = make_browser(page, tmp_path)._application_snapshot(cast(Page, page))

    assert snapshot.screening_form.fields[0].is_required
    assert not snapshot.screening_form.fields[1].is_required


def test_screening_form_hash_covers_safety_flags_and_warnings() -> None:
    field = HhScreeningField(
        "name:telegram",
        "Укажите Telegram",
        "text",
        is_required=True,
    )
    baseline = HhScreeningForm((field,))
    external = HhScreeningForm(
        (
            HhScreeningField(
                field.key,
                field.question,
                field.field_type,
                is_required=True,
                has_external_action=True,
            ),
        )
    )
    warned = HhScreeningForm((field,), warnings=("Важное предупреждение",))

    assert screening_form_hash(baseline) != screening_form_hash(external)
    assert screening_form_hash(baseline) != screening_form_hash(warned)


def test_application_script_scans_the_whole_form_for_dangerous_actions() -> None:
    script = browser_module.APPLICATION_FORM_SCRIPT

    assert "responseDialog = submit?.closest('[role=\"dialog\"]')" in script
    assert "responseScopes.some((scope) => scope.querySelector('input[type=\"file\"]'))" in script
    assert "responseScopes.flatMap" in script
    assert "scope.querySelectorAll('a[href]')" in script
    assert "target.protocol !== 'http:' && target.protocol !== 'https:'" in script
    assert "normalized.includes('домашн')" in script
    assert "(?:пройд\\S*|выполн\\S*)" in script
    assert "test\\s+assignment" in script
    assert "install\\s+" in script
    assert "key.startsWith('__reactFiber$')" in script
    assert "normalizedKey === 'selectedresumehash'" in script
    for warning in (
        browser_module._FORM_ATTACHMENT_WARNING,
        browser_module._FORM_EXTERNAL_LINK_WARNING,
        browser_module._FORM_TEST_ASSIGNMENT_WARNING,
        browser_module._FORM_SOFTWARE_INSTALL_WARNING,
    ):
        assert warning in script


def test_chat_opening_uses_current_card_data_when_vacancy_link_is_absent() -> None:
    script = browser_module.OPEN_NEGOTIATION_CHAT_SCRIPT

    assert "key.startsWith('__reactFiber$')" in script
    assert "fiber.memoizedProps?.topic?.vacancyId" in script
    assert "vacancyIdFor(candidate) === String(vacancyId)" in script


def test_application_form_scripts_have_valid_javascript_syntax() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Для проверки синтаксиса сценариев формы нужен Node.js")
    scripts = (
        browser_module.APPLICATION_FORM_SCRIPT,
        browser_module.FILL_APPLICATION_FORM_SCRIPT,
        browser_module.VERIFY_APPLICATION_FORM_SCRIPT,
    )
    checker = (
        "let input='';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', chunk => input += chunk);"
        "process.stdin.on('end', () => {"
        "for (const script of JSON.parse(input)) new Function(`return (${script})`);"
        "});"
    )

    completed = subprocess.run(
        [node, "-e", checker],
        input=json.dumps(scripts),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_saved_form_answers_are_refilled_without_submit(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    form = HhScreeningForm(
        fields=(
            HhScreeningField(
                key="name:telegram",
                question="Укажите Telegram",
                field_type="text",
                is_required=True,
            ),
        )
    )
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.fill_result = {"filled": ["name:telegram"], "skipped": []}
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        expected_version_hash=screening_form_hash(form),
        answers={"name:telegram": "@timur"},
    )

    assert result.status is HhFormReviewStatus.READY
    assert result.filled_keys == ("name:telegram",)
    assert page.goto_calls == [
        (
            "https://hh.ru/applicant/vacancy_response?"
            "vacancyId=123&startedWithQuestion=false&hhtmFrom=vacancy",
            "domcontentloaded",
        )
    ]
    assert page.locators['[data-qa="vacancy-response-link-top"]:visible'].clicked == 0
    assert page.fill_payload == [{"key": "name:telegram", "value": "@timur"}]
    assert submit.clicked == 0


def test_salary_answer_is_refilled_when_textarea_is_sibling_of_question(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    question = "Добрый день, уважаемый соискатель! Укажите пожалуйста ваши зарплатные ожидания"
    field_key = f"question:0:{question.casefold()}"
    legacy_form = HhScreeningForm(
        fields=(
            HhScreeningField(
                key=field_key,
                question=question,
                field_type="unknown",
            ),
        )
    )
    page.application_payload = {
        "fields": [
            {
                "key": field_key,
                "question": question,
                "fieldType": "textarea",
                "isRequired": False,
                "options": [],
                "maxLength": None,
                "formatHint": "",
                "controlOutsideQuestion": True,
                "hasAttachment": False,
                "hasExternalAction": False,
                "hasTestAssignment": False,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.fill_result = {"filled": [field_key], "skipped": []}
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/135702392",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        expected_version_hash=screening_form_hash(legacy_form),
        answers={field_key: "от 120 000 рублей на руки"},
    )

    assert result.status is HhFormReviewStatus.READY
    assert result.current_form is not None
    assert result.current_form.fields[0].field_type == "textarea"
    assert result.filled_keys == (field_key,)
    assert result.skipped_keys == ()
    assert page.fill_payload == [{"key": field_key, "value": "от 120 000 рублей на руки"}]
    assert submit.clicked == 0


def test_saved_form_refills_cover_letter_and_reports_skipped_answer(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    form = HhScreeningForm(
        fields=(HhScreeningField("name:telegram", "Укажите Telegram", "text", True),)
    )
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.fill_result = {"filled": [], "skipped": ["name:telegram"]}
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)
    letter = FakeLocator()
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = letter

    result = make_browser(page, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        expected_version_hash=screening_form_hash(form),
        answers={"name:telegram": "@timur"},
        cover_letter="Здравствуйте!",
    )

    assert result.status is HhFormReviewStatus.READY
    assert result.skipped_keys == ("name:telegram",)
    assert letter.filled == ["Здравствуйте!"]


@pytest.mark.parametrize(
    ("url", "body_text", "resume_title", "fields", "captcha", "expected_status"),
    (
        (
            "https://hh.ru/account/login",
            "Форма отклика",
            "Python",
            True,
            False,
            HhFormReviewStatus.AUTH_REQUIRED,
        ),
        (
            "https://hh.ru/applicant/resumes",
            "Форма отклика",
            "Python",
            True,
            True,
            HhFormReviewStatus.CAPTCHA_REQUIRED,
        ),
        (
            "https://hh.ru/applicant/resumes",
            "Вакансия закрыта",
            "Python",
            True,
            False,
            HhFormReviewStatus.VACANCY_CLOSED,
        ),
        (
            "https://hh.ru/applicant/resumes",
            "Вы уже откликались",
            "Python",
            True,
            False,
            HhFormReviewStatus.ALREADY_APPLIED,
        ),
        (
            "https://hh.ru/applicant/resumes",
            "Форма отклика",
            "Другое резюме",
            True,
            False,
            HhFormReviewStatus.RESUME_MISMATCH,
        ),
        (
            "https://hh.ru/applicant/resumes",
            "Форма отклика",
            "Python",
            False,
            False,
            HhFormReviewStatus.UNAVAILABLE,
        ),
    ),
)
def test_form_review_stops_on_unsafe_page_state(
    tmp_path: Path,
    url: str,
    body_text: str,
    resume_title: str,
    fields: bool,
    captcha: bool,
    expected_status: HhFormReviewStatus,
) -> None:
    page = FakePage(url)
    page.goto_final_url = url
    page.application_payload = {
        "fields": (
            [
                {
                    "key": "name:telegram",
                    "question": "Укажите Telegram",
                    "fieldType": "text",
                    "isRequired": True,
                    "options": [],
                    "maxLength": None,
                }
            ]
            if fields
            else []
        ),
        "warnings": [],
        "resumeTitle": resume_title,
        "bodyText": body_text,
    }
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(
        1 if captcha else 0,
        visible=captcha,
    )
    form = HhScreeningForm(
        fields=(HhScreeningField("name:telegram", "Укажите Telegram", "text", True),)
    )

    result = make_browser(page, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        expected_version_hash=screening_form_hash(form),
        answers={},
    )

    assert result.status is expected_status
    assert page.fill_payload is None


def test_form_review_handles_navigation_timeout_and_rate_limit(tmp_path: Path) -> None:
    timed_out = FakePage("https://hh.ru/applicant/resumes")
    timed_out.goto_error = TimeoutError("wait")
    timeout_result = make_browser(timed_out, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        expected_version_hash="version",
        answers={},
    )
    assert timeout_result.status is HhFormReviewStatus.UNAVAILABLE

    limited = FakePage("https://hh.ru/applicant/resumes")
    limited.goto_response = FakeResponse()
    limited.goto_response.status = 429
    limit_result = make_browser(limited, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        expected_version_hash="version",
        answers={},
    )
    assert limit_result.status is HhFormReviewStatus.UNAVAILABLE
    assert "ограничил" in limit_result.message


@pytest.mark.parametrize(
    ("body_text", "expected"),
    (
        ("Отклик успешно отправлен", HhFormReviewStatus.ALREADY_APPLIED),
        ("Вакансия в архиве", HhFormReviewStatus.VACANCY_CLOSED),
        ("Форма отклика", None),
    ),
)
def test_current_screening_form_status_detects_manual_completion(
    tmp_path: Path,
    body_text: str,
    expected: HhFormReviewStatus | None,
) -> None:
    page = FakePage(
        "https://hh.ru/applicant/vacancy_response?"
        "vacancyId=123&startedWithQuestion=false&hhtmFrom=vacancy"
    )
    page.locators["body"] = FakeLocator(text=body_text)

    status = make_browser(page, tmp_path).current_screening_form_status(
        "https://hh.ru/vacancy/123"
    )

    assert status is expected


@pytest.mark.parametrize(
    "fill_result",
    (None, {"filled": "bad", "skipped": []}, {"filled": [], "skipped": "bad"}),
)
def test_form_review_rejects_invalid_fill_result(
    tmp_path: Path,
    fill_result: object,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    form = HhScreeningForm(
        fields=(HhScreeningField("name:telegram", "Укажите Telegram", "text", True),)
    )
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python",
        "bodyText": "Форма отклика",
    }
    page.fill_result = fill_result
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)

    with pytest.raises(RuntimeError):
        make_browser(page, tmp_path).open_screening_form(
            "https://hh.ru/vacancy/123",
            expected_resume_hh_id=TEST_RESUME_HH_ID,
            expected_resume_title="Python",
            expected_version_hash=screening_form_hash(form),
            answers={"name:telegram": "@timur"},
        )


def test_changed_form_is_not_refilled(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "fields": [
            {
                "key": "name:new-question",
                "question": "Новый вопрос",
                "fieldType": "textarea",
                "isRequired": True,
                "options": [],
                "maxLength": None,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)

    result = make_browser(page, tmp_path).open_screening_form(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        expected_version_hash="old-version",
        answers={"name:old-question": "Старый ответ"},
    )

    assert result.status is HhFormReviewStatus.FORM_CHANGED
    assert page.fill_payload is None


def test_application_is_submitted_once_after_all_checks(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": ["Город не указан"],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    toggle_selector = '[data-qa="add-cover-letter"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator(0)
    page.locators[toggle_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator()
    page.locators["body"] = FakeLocator(text="Отклик отправлен")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Содержательное письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.APPLIED
    assert result.confirmation == "hh.ru подтвердил отправку отклика"
    assert result.warnings == ("Город не указан",)
    assert page.locators[toggle_selector].clicked == 1
    assert page.locators[letter_selector].filled == ["Содержательное письмо"]
    assert page.locators[submit_selector].clicked == 1
    assert page.locators[submit_selector].no_wait_after == [True]
    assert page.locators[submit_selector].trial_clicks == [True, False]


def test_confirmed_simple_screening_form_is_rechecked_and_submitted(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    form = HhScreeningForm(
        fields=(
            HhScreeningField(
                "name:telegram",
                "Укажите Telegram",
                "text",
                is_required=True,
            ),
        )
    )
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
                "formatHint": "",
                "hasAttachment": False,
                "hasExternalAction": False,
                "hasTestAssignment": False,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.fill_result = {"filled": ["name:telegram"], "skipped": []}
    page.verify_result = {"missingRequired": [], "mismatched": []}
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    page.locators["body"] = FakeLocator(text="Отклик отправлен")
    submission = HhScreeningSubmission(
        screening_form_hash(form),
        (("name:telegram", "@timur"),),
    )

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Содержательное письмо",
        submit=True,
        submit_guard=lambda: True,
        screening_submission=submission,
    )

    assert result.status is HhApplyStatus.APPLIED
    assert result.screening_form_version_hash == submission.version_hash
    assert page.fill_payload == [{"key": "name:telegram", "value": "@timur"}]
    assert page.verify_payload == [{"key": "name:telegram", "value": "@timur"}]
    assert submit.clicked == 1


@pytest.mark.parametrize(
    ("new_question", "expected_status"),
    [
        ("Паспортные данные", HhApplyStatus.MANUAL_REVIEW_REQUIRED),
        ("Укажите город", HhApplyStatus.QUESTIONS_REQUIRED),
    ],
)
def test_form_change_after_repeated_fill_stops_submission(
    tmp_path: Path,
    new_question: str,
    expected_status: HhApplyStatus,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    safe_field = {
        "key": "name:telegram",
        "question": "Укажите Telegram",
        "fieldType": "text",
        "isRequired": True,
        "options": [],
        "maxLength": None,
        "formatHint": "",
        "hasAttachment": False,
        "hasExternalAction": False,
        "hasTestAssignment": False,
    }
    new_field = {
        "key": "name:new-field",
        "question": new_question,
        "fieldType": "text",
        "isRequired": True,
        "options": [],
        "maxLength": None,
        "formatHint": "",
        "hasAttachment": False,
        "hasExternalAction": False,
        "hasTestAssignment": False,
    }
    safe_payload = {
        "fields": [safe_field],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    changed_payload = {**safe_payload, "fields": [safe_field, new_field]}
    page.application_payloads = [
        safe_payload,
        safe_payload,
        changed_payload,
    ]
    page.fill_result = {"filled": ["name:telegram"], "skipped": []}
    page.verify_result = {"missingRequired": [], "mismatched": []}
    page.locators['[data-qa*="captcha"], iframe[src*="captcha"]'] = FakeLocator(0)
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    form = HhScreeningForm(
        fields=(
            HhScreeningField(
                "name:telegram",
                "Укажите Telegram",
                "text",
                is_required=True,
            ),
        )
    )

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
        screening_submission=HhScreeningSubmission(
            screening_form_hash(form),
            (("name:telegram", "@timur"),),
        ),
    )

    assert result.status is expected_status
    assert page.application_payload_index == 3
    assert submit.clicked == 0
    assert submit.trial_clicks == []


def test_redirect_to_another_vacancy_stops_before_opening_form(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.goto_final_url = "https://hh.ru/vacancy/999"
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert "123" in result.confirmation
    assert page.locators['[data-qa="vacancy-response-link-top"]:visible'].clicked == 0
    assert submit.clicked == 0


def test_form_for_another_vacancy_is_not_submitted(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
        "vacancyId": "999",
    }
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert submit.clicked == 0


def test_submission_request_for_another_vacancy_is_aborted(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.response.url = "https://hh.ru/applicant/vacancy_response?vacancyId=999"
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    page.locators["body"] = FakeLocator(text="")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert submit.clicked == 1
    assert page.last_route is not None and page.last_route.aborted


def test_changed_screening_form_is_not_submitted_automatically(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    current = HhScreeningForm(
        fields=(
            HhScreeningField(
                "name:telegram",
                "Укажите другой контакт",
                "text",
                is_required=True,
            ),
        )
    )
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите другой контакт",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
                "formatHint": "",
                "hasAttachment": False,
                "hasExternalAction": False,
                "hasTestAssignment": False,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    previous = HhScreeningSubmission(
        "0" * 64,
        (("name:telegram", "@timur"),),
    )

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
        screening_submission=previous,
    )

    assert result.status is HhApplyStatus.QUESTIONS_REQUIRED
    assert result.screening_form == current
    assert page.fill_payload is None
    assert submit.clicked == 0


def test_unfilled_confirmed_answer_stops_automatic_screening_submission(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    form = HhScreeningForm(
        fields=(
            HhScreeningField(
                "name:telegram",
                "Укажите Telegram",
                "text",
                is_required=True,
            ),
        )
    )
    page.application_payload = {
        "fields": [
            {
                "key": "name:telegram",
                "question": "Укажите Telegram",
                "fieldType": "text",
                "isRequired": True,
                "options": [],
                "maxLength": None,
                "formatHint": "",
                "hasAttachment": False,
                "hasExternalAction": False,
                "hasTestAssignment": False,
            }
        ],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.fill_result = {"filled": [], "skipped": ["name:telegram"]}
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
        screening_submission=HhScreeningSubmission(
            screening_form_hash(form),
            (("name:telegram", "@timur"),),
        ),
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert result.screening_form == form
    assert "Не все" in result.confirmation
    assert submit.clicked == 0


def test_application_respects_retry_after_header(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.response.status = 429
    page.response.headers["retry-after"] = "120"
    page.goto_response = page.response
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()
    page.locators["body"] = FakeLocator(text="Слишком много запросов")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert result.retry_after_seconds == 120


def test_application_http_403_requires_manual_account_review(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    response = FakeResponse()
    response.status = 403
    page.goto_response = response

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.ACCOUNT_WARNING
    assert result.retry_after_seconds is None
    assert "ручная проверка" in result.confirmation


def test_application_http_403_for_inaccessible_vacancy_skips_only_vacancy(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    response = FakeResponse()
    response.status = 403
    page.goto_response = response
    page.locators["body"] = FakeLocator(
        text=(
            "Вам недоступна эта вакансия. Войдите как пользователь, "
            "у которого есть доступ на просмотр."
        )
    )

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.VACANCY_CLOSED


@pytest.mark.parametrize(
    ("body_text", "expected_delay"),
    [
        ("Слишком много запросов. Повторите позже.", 15 * 60),
        ("Достигнут лимит откликов на сегодня.", 24 * 60 * 60),
    ],
)
def test_application_text_limits_are_retried_automatically(
    tmp_path: Path,
    body_text: str,
    expected_delay: int,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators["body"] = FakeLocator(text=body_text)

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert result.retry_after_seconds == expected_delay
    assert "автоматически" in result.confirmation


@pytest.mark.parametrize(
    "body_text",
    [
        "Подозрительная активность. Подтвердите аккаунт.",
        "Аккаунт заблокирован.",
    ],
)
def test_application_real_account_warning_requires_manual_review(
    tmp_path: Path,
    body_text: str,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators["body"] = FakeLocator(text=body_text)

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.ACCOUNT_WARNING
    assert result.retry_after_seconds is None


def test_application_aborts_request_with_unexpected_resume(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "resumeHhId": "",
        "bodyText": "Форма отклика",
    }
    page.response.request.post_data = "resumeHash=unexpected-resume"
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert "номера вакансии и резюме" in result.confirmation
    assert page.last_route is not None and page.last_route.aborted
    assert submit.clicked == 1
    assert page.route_handler is None


def test_application_accepts_hidden_resume_id_when_actual_request_matches(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "resumeHhId": "",
        "bodyText": "Форма отклика",
    }
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.APPLIED
    assert page.last_route is not None and not page.last_route.aborted
    assert submit.clicked == 1


def test_application_uses_history_when_response_body_is_unavailable(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()
    page.negotiations_payload = [
        {
            "vacancyId": "123",
            "vacancyHref": "/vacancy/123",
            "statusQa": "negotiations-tag negotiations-item-not-viewed",
            "statusLabel": "Не просмотрен",
            "chatAvailable": False,
        }
    ]
    page.response.text_error = Error("response body unavailable")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://ufa.hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.APPLIED
    assert result.confirmation == "Отклик найден в истории hh.ru"
    assert page.locators['[data-qa="vacancy-response-submit-popup"]'].clicked == 1
    assert page.goto_calls[0][0] == "https://hh.ru/vacancy/123"
    assert page.goto_calls[-1][0] == "https://hh.ru/applicant/negotiations"


def test_unexpected_form_payload_before_submit_is_retryable(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {"unexpected": "payload"}
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[submit_selector] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert "до нажатия" in result.confirmation
    assert page.locators[submit_selector].clicked == 0


def test_application_marks_unknown_result_without_confirmation(tmp_path: Path) -> None:
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
    page.negotiations_payload = [
        {
            "vacancyId": "999",
            "vacancyHref": "/vacancy/999",
            "statusQa": "negotiations-tag negotiations-item-not-viewed",
            "statusLabel": "Не просмотрен",
            "chatAvailable": False,
        }
    ]

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.UNKNOWN_RESULT
    assert "нажата один раз" in result.confirmation
    assert page.locators['[data-qa="vacancy-response-submit-popup"]'].clicked == 1
    assert page.goto_calls[-1][0] == "https://hh.ru/applicant/negotiations"


@pytest.mark.parametrize(
    ("status", "expected_status", "retry_after"),
    [
        (429, HhApplyStatus.RETRYABLE_ERROR, 73),
        (403, HhApplyStatus.ACCOUNT_WARNING, None),
    ],
)
def test_application_classifies_post_limit_and_security_response_after_click(
    tmp_path: Path,
    status: int,
    expected_status: HhApplyStatus,
    retry_after: int | None,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.response.status = status
    page.response.headers["retry-after"] = "73"
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    page.locators["body"] = FakeLocator(text="Форма отклика")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is expected_status
    assert result.retry_after_seconds == retry_after
    assert submit.clicked == 1
    assert page.goto_calls == [("https://hh.ru/vacancy/123", "domcontentloaded")]


def test_application_security_warning_after_click_blocks_account(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.response.body = "Подозрительная активность. Подтвердите аккаунт."
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    page.locators["body"] = FakeLocator(text="Форма отклика")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.ACCOUNT_WARNING
    assert submit.clicked == 1


@pytest.mark.parametrize(
    ("changed_key", "changed_value", "expected_status", "confirmation"),
    [
        ("resumeHhId", "другое-резюме", HhApplyStatus.RESUME_MISMATCH, "номером"),
        (
            "coverLetter",
            "Подменённый текст",
            HhApplyStatus.MANUAL_REVIEW_REQUIRED,
            "Текст письма",
        ),
    ],
)
def test_application_rechecks_exact_resume_id_and_letter_immediately_before_click(
    tmp_path: Path,
    changed_key: str,
    changed_value: str,
    expected_status: HhApplyStatus,
    confirmation: str,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    safe_payload: dict[str, object] = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "resumeHhId": TEST_RESUME_HH_ID,
        "coverLetter": "Письмо",
        "bodyText": "Форма отклика",
    }
    changed_payload = dict(safe_payload)
    changed_payload[changed_key] = changed_value
    page.application_payloads = [
        safe_payload,
        safe_payload,
        safe_payload,
        changed_payload,
    ]
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is expected_status
    assert confirmation in result.confirmation
    assert submit.clicked == 0
    assert submit.trial_clicks == [True]


@pytest.mark.parametrize(
    "warning",
    [
        browser_module._FORM_ATTACHMENT_WARNING,
        browser_module._FORM_EXTERNAL_LINK_WARNING,
        browser_module._FORM_TEST_ASSIGNMENT_WARNING,
        browser_module._FORM_SOFTWARE_INSTALL_WARNING,
    ],
)
def test_form_wide_danger_without_task_question_blocks_application(
    tmp_path: Path,
    warning: str,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [warning],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert result.warnings == (warning,)
    assert submit.clicked == 0


def test_form_wide_danger_is_checked_again_after_trial_click(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    safe_payload: dict[str, object] = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    dangerous_payload = {
        **safe_payload,
        "warnings": [browser_module._FORM_EXTERNAL_LINK_WARNING],
    }
    page.application_payloads = [
        safe_payload,
        safe_payload,
        safe_payload,
        dangerous_payload,
    ]
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert result.warnings == (browser_module._FORM_EXTERNAL_LINK_WARNING,)
    assert submit.clicked == 0
    assert submit.trial_clicks == [True]


def test_application_preview_fills_letter_without_submit(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "resumeHhId": "",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо для проверки",
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert page.locators[letter_selector].filled == ["Письмо для проверки"]
    assert page.locators[submit_selector].clicked == 0


def test_application_submit_guard_is_checked_before_click(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо для проверки",
        submit=True,
        submit_guard=lambda: False,
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert page.locators[submit_selector].clicked == 0


def test_application_submit_guard_failure_is_retryable(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator()

    def failed_guard() -> bool:
        raise RuntimeError("database unavailable")

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=failed_guard,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert "повторно проверить" in result.confirmation
    assert page.locators[submit_selector].clicked == 0


def test_submit_button_actionability_timeout_is_retryable(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator(click_error=True)

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
        submit=True,
        submit_guard=lambda: True,
    )

    assert result.status is HhApplyStatus.RETRYABLE_ERROR
    assert "до нажатия" in result.confirmation
    assert page.locators[submit_selector].clicked == 0


def test_application_cannot_submit_without_guard(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python backend разработчик",
        "bodyText": "Форма отклика",
    }
    letter_selector = '[data-qa="vacancy-response-popup-form-letter-input"]'
    submit_selector = '[data-qa="vacancy-response-submit-popup"]'
    page.locators[letter_selector] = FakeLocator()
    page.locators[submit_selector] = FakeLocator()

    result = make_browser(page, tmp_path).apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо для проверки",
        submit=True,
    )

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert page.locators[submit_selector].clicked == 0


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
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend разработчик",
        cover_letter="Письмо",
    )

    assert result.status is HhApplyStatus.ALREADY_APPLIED
    assert submit.clicked == 0


def test_confirmed_recruiter_message_is_sent_once(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    message_body = "Спасибо, буду на связи."
    frame = FakeFrame(
        messages_payloads=[
            [],
            [
                {
                    "messageId": "message-7",
                    "direction": "OUTGOING",
                    "body": message_body,
                }
            ],
        ]
    )
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"
    page.response.body = '{"id":"message-7"}'

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        f" {message_body} ",
    )

    assert result.outcome is MessageSendOutcome.SENT
    assert result.external_id == "message-7"
    assert page.opened_vacancy_ids == ["101"]
    assert frame.locators['[data-qa="chatik-new-message-text"]'].filled == [message_body]
    assert frame.locators['[data-qa="chatik-do-send-message"]'].clicked == 1
    assert frame.locators['[data-qa="chatik-do-send-message"]'].no_wait_after == [True]


def test_recruiter_message_waits_until_filled_text_enables_send(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    message_body = "Спасибо, буду на связи."
    frame = FakeFrame(
        messages_payloads=[
            [],
            [
                {
                    "messageId": "message-7",
                    "direction": "OUTGOING",
                    "body": message_body,
                }
            ],
        ]
    )
    submit = FakeLocator(enabled=False)
    editor = FakeLocator(on_fill=lambda _value: setattr(submit, "enabled", True))
    frame.locators['[data-qa="chatik-new-message-text"]'] = editor
    frame.locators['[data-qa="chatik-do-send-message"]'] = submit
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        message_body,
    )

    assert result.outcome is MessageSendOutcome.SENT
    assert submit.clicked == 1


def test_recruiter_message_finds_chat_on_later_negotiations_page(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.opened_chat = False
    first_page = FakeLocator(text="1")
    second_page = FakeLocator(
        text="2",
        on_click=lambda: setattr(page, "opened_chat", True),
    )
    page.locators['[data-qa^="number-pages-"]'] = FakeLocator(
        items=[first_page, second_page]
    )
    page.locators['[data-qa*="number-pages-selected"]'] = first_page
    page.locators['[data-qa^="number-pages-1"]'] = first_page
    page.locators['[data-qa^="number-pages-2"]'] = second_page
    message_body = "Спасибо, буду на связи."
    frame = FakeFrame(
        messages_payloads=[
            [],
            [
                {
                    "messageId": "message-7",
                    "direction": "OUTGOING",
                    "body": message_body,
                }
            ],
        ]
    )
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        message_body,
    )

    assert result.outcome is MessageSendOutcome.SENT
    assert first_page.clicked == 0
    assert second_page.clicked == 1


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            [
                {
                    "messageId": "old-message",
                    "direction": "OUTGOING",
                    "body": "Still interested.",
                }
            ],
            [
                {
                    "messageId": "old-message",
                    "direction": "OUTGOING",
                    "body": "Still interested.",
                }
            ],
        ),
        (
            [],
            [
                {
                    "messageId": "incoming-message",
                    "direction": "INCOMING",
                    "body": "Still interested.",
                }
            ],
        ),
    ],
)
def test_matching_old_or_incoming_message_does_not_confirm_send(
    tmp_path: Path,
    before: list[dict[str, str]],
    after: list[dict[str, str]],
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    frame = FakeFrame(messages_payloads=[before, after])
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"
    page.response.body = '{"id":"response-message"}'

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        "Still interested.",
    )

    assert result.outcome is MessageSendOutcome.UNKNOWN_RESULT
    assert result.external_id is None
    assert frame.locators['[data-qa="chatik-do-send-message"]'].clicked == 1


def test_new_outgoing_message_id_confirms_send_when_count_is_unchanged(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    before = [{"messageId": "old-message", "direction": "OUTGOING", "body": "Reply"}]
    after = [{"messageId": "new-message", "direction": "OUTGOING", "body": "Reply"}]
    frame = FakeFrame(messages_payloads=[before, after])
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        "Reply",
    )

    assert result.outcome is MessageSendOutcome.SENT
    assert result.external_id == "new-message"


def test_increased_exact_outgoing_count_confirms_send_without_new_id(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    message = {"messageId": "same-message", "direction": "OUTGOING", "body": "Reply"}
    frame = FakeFrame(messages_payloads=[[message], [message, message]])
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"
    page.response.body = '{"id":"response-message"}'

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        "Reply",
    )

    assert result.outcome is MessageSendOutcome.SENT
    assert result.external_id == "response-message"


def test_message_is_not_sent_without_initial_outgoing_snapshot(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    frame = FakeFrame(messages_payload={"unexpected": "payload"})
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        "Reply",
    )

    assert result.outcome is MessageSendOutcome.FAILED
    assert frame.locators['[data-qa="chatik-do-send-message"]'].clicked == 0


def test_missing_outgoing_snapshot_after_click_keeps_unknown_result(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    frame = FakeFrame(messages_payloads=[[], {"unexpected": "payload"}])
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = FakeLocator()
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        "Reply",
    )

    assert result.outcome is MessageSendOutcome.UNKNOWN_RESULT
    assert frame.locators['[data-qa="chatik-do-send-message"]'].clicked == 1


@pytest.mark.parametrize("status", [403, 429])
def test_recruiter_message_propagates_access_error_before_click(
    tmp_path: Path,
    status: int,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    response = FakeResponse()
    response.status = status
    response.headers["retry-after"] = "41"
    page.goto_response = response

    if status == 403:
        with pytest.raises(HhSyncBlockedError) as blocked_error:
            make_browser(page, tmp_path).send_recruiter_message(
                "https://hh.ru/vacancy/101",
                "Ответ",
            )
        assert blocked_error.value.code == "ACCOUNT_WARNING"
    else:
        with pytest.raises(HhSyncRetryableError) as retryable_error:
            make_browser(page, tmp_path).send_recruiter_message(
                "https://hh.ru/vacancy/101",
                "Ответ",
            )
        assert retryable_error.value.code == "HH_RATE_LIMITED"
        assert retryable_error.value.retry_after_seconds == 41


@pytest.mark.parametrize("status", [403, 429])
def test_recruiter_message_propagates_post_access_error_after_click(
    tmp_path: Path,
    status: int,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    frame = FakeFrame(messages_payloads=[[], []])
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    submit = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = submit
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"
    page.response.status = status
    page.response.headers["retry-after"] = "59"
    page.locators["body"] = FakeLocator(text="Переписка")

    if status == 403:
        with pytest.raises(HhSyncBlockedError) as blocked_error:
            make_browser(page, tmp_path).send_recruiter_message(
                "https://hh.ru/vacancy/101",
                "Ответ",
            )
        assert blocked_error.value.code == "ACCOUNT_WARNING"
    else:
        with pytest.raises(HhSyncRetryableError) as retryable_error:
            make_browser(page, tmp_path).send_recruiter_message(
                "https://hh.ru/vacancy/101",
                "Ответ",
            )
        assert retryable_error.value.code == "HH_RATE_LIMITED"
        assert retryable_error.value.retry_after_seconds == 59
    assert submit.clicked == 1


def test_recruiter_message_security_warning_after_click_blocks_account(
    tmp_path: Path,
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    frame = FakeFrame(messages_payloads=[[], []])
    frame.locators['[data-qa="chatik-new-message-text"]'] = FakeLocator()
    submit = FakeLocator()
    frame.locators['[data-qa="chatik-do-send-message"]'] = submit
    page.frames = [cast(Frame, frame)]
    page.response.url = "https://hh.ru/chat/101/messages"
    page.response.body = "Подозрительная активность. Подтвердите аккаунт."
    page.locators["body"] = FakeLocator(text="Переписка")

    with pytest.raises(HhSyncBlockedError) as error:
        make_browser(page, tmp_path).send_recruiter_message(
            "https://hh.ru/vacancy/101",
            "Ответ",
        )

    assert error.value.code == "ACCOUNT_WARNING"
    assert submit.clicked == 1


def test_recruiter_message_is_not_sent_without_unique_chat(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.opened_chat = False

    result = make_browser(page, tmp_path).send_recruiter_message(
        "https://hh.ru/vacancy/101",
        "Здравствуйте!",
    )

    assert result.outcome is MessageSendOutcome.FAILED


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


def test_challenge_has_priority_over_authenticated_page(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators[browser_module._CAPTCHA_SELECTOR] = FakeLocator(visible=True)

    assert (
        make_browser(page, tmp_path)._classify(cast(Page, page))
        is LoginStatus.CAPTCHA_REQUIRED
    )


def test_authenticated_page_has_priority_over_form_states(tmp_path: Path) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.locators['[data-qa="form-helper-error"]'] = FakeLocator(visible=True)

    assert make_browser(page, tmp_path)._classify(cast(Page, page)) is LoginStatus.AUTHENTICATED


def test_missing_password_field_retries_without_resubmitting_credentials(
    tmp_path: Path,
) -> None:
    page = FakePage()
    prepare_login_page(page, password_error=True)

    with pytest.raises(HhSyncRetryableError) as error:
        make_browser(page, tmp_path).submit_credentials(
            HhCredentials("person@example.com", "secret")
        )

    assert error.value.code == "HH_LOGIN_FORM_TIMEOUT"
    assert error.value.retry_after_seconds == browser_module._NETWORK_RETRY_SECONDS
    assert page.locators['[data-qa="submit-button"]'].clicked == 0


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


def test_profile_lock_waits_until_another_browser_releases_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "profile.lock"
    owner = browser_module._BrowserProfileLock(lock_path, timeout_seconds=1)
    contender = browser_module._BrowserProfileLock(lock_path, timeout_seconds=1)
    owner.acquire()
    waits: list[float] = []

    def release_owner(delay: float) -> None:
        waits.append(delay)
        owner.release()

    monkeypatch.setattr(browser_module, "sleep", release_owner)
    contender.acquire()
    contender.release()

    assert waits == [browser_module._PROFILE_LOCK_RETRY_SECONDS]


def test_profile_lock_wait_is_bounded(tmp_path: Path) -> None:
    lock_path = tmp_path / "profile.lock"
    owner = browser_module._BrowserProfileLock(lock_path, timeout_seconds=0)
    contender = browser_module._BrowserProfileLock(lock_path, timeout_seconds=0)
    owner.acquire()

    try:
        with pytest.raises(RuntimeError, match="занят другой задачей"):
            contender.acquire()
    finally:
        owner.release()


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
        assert chromium.calls[0]["args"] == ["--start-maximized"]
        contender = browser_module._BrowserProfileLock(
            tmp_path / "profile" / browser_module._PROFILE_LOCK_FILENAME,
            timeout_seconds=0,
        )
        with pytest.raises(RuntimeError, match="занят другой задачей"):
            contender.acquire()

    assert (tmp_path / "profile").is_dir()
    assert context.closed
    assert playwright.stopped
    contender.acquire()
    contender.release()


def test_background_context_starts_minimized_with_quiet_chromium(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = FakePage()
    context = FakeContext(page)
    chromium = FakeChromium(context)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(browser_module, "sync_playwright", lambda: FakeStarter(playwright))

    with VisibleHhBrowser(
        tmp_path / "profile",
        "https://hh.ru/account/login",
        "https://hh.ru/applicant/resumes",
        "https://hh.ru/search/vacancy",
        4_000,
        start_minimized=True,
    ):
        arguments = cast(list[str], chromium.calls[0]["args"])
        assert arguments[0] == "--start-minimized"
        assert "--mute-audio" in arguments
        assert "--start-maximized" not in arguments


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("hh.ru", True),
        ("chatik.hh.ru", True),
        ("api.hh.ru", True),
        ("hhcdn.ru", True),
        ("img.hhcdn.ru", True),
        ("evilhh.ru", False),
        ("hh.ru.example.com", False),
        ("hhcdn.ru.example.com", False),
        ("example.com", False),
    ],
)
def test_hh_proxy_accepts_only_hh_domains(host: str, allowed: bool) -> None:
    assert browser_module._HhHttpProxy._allowed_host(host) is allowed


def test_background_context_can_relay_hh_through_selected_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, ...]] = []

    class FakeProxy:
        def __init__(self, source_host: str) -> None:
            events.append(("created", source_host))

        @property
        def pac_url(self) -> str:
            return "http://127.0.0.1:43123/proxy.pac"

        def start(self) -> None:
            events.append(("started",))

        def stop(self) -> None:
            events.append(("stopped",))

    page = FakePage()
    context = FakeContext(page)
    chromium = FakeChromium(context)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(browser_module, "sync_playwright", lambda: FakeStarter(playwright))
    monkeypatch.setattr(browser_module, "_HhHttpProxy", FakeProxy)
    monkeypatch.setattr(browser_module, "usable_source_ipv4", lambda value: value)

    with VisibleHhBrowser(
        tmp_path / "profile",
        "https://hh.ru/account/login",
        "https://hh.ru/applicant/resumes",
        "https://hh.ru/search/vacancy",
        4_000,
        start_minimized=True,
        browser_source_ip="192.168.0.18",
    ):
        arguments = cast(list[str], chromium.calls[0]["args"])
        assert "--proxy-pac-url=http://127.0.0.1:43123/proxy.pac" in arguments
        assert not any(argument.startswith("--host-resolver-rules=") for argument in arguments)

    assert events == [
        ("created", "192.168.0.18"),
        ("started",),
        ("stopped",),
    ]


def test_background_context_uses_direct_network_when_source_address_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakePage()
    context = FakeContext(page)
    chromium = FakeChromium(context)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(browser_module, "sync_playwright", lambda: FakeStarter(playwright))
    monkeypatch.setattr(browser_module, "usable_source_ipv4", lambda _value: None)

    with VisibleHhBrowser(
        tmp_path / "profile",
        "https://hh.ru/account/login",
        "https://hh.ru/applicant/resumes",
        "https://hh.ru/search/vacancy",
        4_000,
        start_minimized=True,
        browser_source_ip="192.168.0.18",
    ):
        arguments = cast(list[str], chromium.calls[0]["args"])
        assert not any(argument.startswith("--proxy-pac-url=") for argument in arguments)


def test_hh_proxy_uses_doh_and_caches_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str | None, int]] = []

    def no_system_dns(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror("DNS unavailable")

    def answer_with_doh(
        request: Request,
        *,
        timeout: int,
    ) -> BytesIO:
        requests.append(
            (
                request.full_url,
                request.get_header("Accept"),
                timeout,
            )
        )
        return BytesIO(
            b'{"Answer":['
            b'{"type":5,"data":"chatik.hh.ru.cdn.cloudflare.net."},'
            b'{"type":1,"data":"94.124.200.1"}'
            b"]}"
        )

    monkeypatch.setattr(socket, "getaddrinfo", no_system_dns)
    monkeypatch.setattr(urllib.request, "urlopen", answer_with_doh)
    proxy = browser_module._HhHttpProxy("192.168.0.18")

    assert proxy._resolve_host("chatik.hh.ru") == "94.124.200.1"
    assert proxy._resolve_host("chatik.hh.ru") == "94.124.200.1"
    assert requests == [
        (
            "https://cloudflare-dns.com/dns-query?name=chatik.hh.ru&type=A",
            "application/dns-json",
            10,
        )
    ]


def test_hh_proxy_falls_back_to_system_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("DoH unavailable")),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("94.124.200.0", 443))
        ],
    )

    proxy = browser_module._HhHttpProxy("192.168.0.18")

    assert proxy._resolve_host("hh.ru") == "94.124.200.0"


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
    contender = browser_module._BrowserProfileLock(
        tmp_path / browser_module._PROFILE_LOCK_FILENAME,
        timeout_seconds=0,
    )
    contender.acquire()
    contender.release()


def test_profile_lock_rejects_second_acquire_and_allows_empty_release(tmp_path: Path) -> None:
    lock = browser_module._BrowserProfileLock(tmp_path / "profile.lock", timeout_seconds=0)

    lock.release()
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="уже занят"):
            lock.acquire()
    finally:
        lock.release()


def test_profile_lock_uses_posix_file_locking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[int, int]] = []
    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_NB=2,
        LOCK_UN=4,
        flock=lambda descriptor, operation: calls.append((descriptor, operation)),
    )
    monkeypatch.setattr(browser_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(browser_module, "fcntl", fake_fcntl, raising=False)

    with (tmp_path / "profile.lock").open("a+b") as handle:
        browser_module._BrowserProfileLock._try_lock(handle)
        browser_module._BrowserProfileLock._unlock(handle)
        descriptor = handle.fileno()

    assert calls == [(descriptor, 3), (descriptor, 4)]


def test_retry_after_accepts_http_date_without_timezone() -> None:
    response = FakeResponse()
    response.headers["retry-after"] = "Wed, 31 Dec 2099 23:59:59"

    seconds = VisibleHhBrowser._retry_after_seconds(cast(Response, response))

    assert seconds is not None
    assert seconds > 86_400


def test_retry_after_never_shortens_numeric_platform_delay() -> None:
    response = FakeResponse()
    response.headers["retry-after"] = "604800"

    assert VisibleHhBrowser._retry_after_seconds(cast(Response, response)) == 604_800


@pytest.mark.parametrize("value", [None, "not-a-date"])
def test_retry_after_ignores_missing_or_invalid_value(value: str | None) -> None:
    response = FakeResponse()
    if value is not None:
        response.headers["retry-after"] = value

    assert VisibleHhBrowser._retry_after_seconds(cast(Response, response)) is None


def test_retry_after_ignores_unavailable_headers() -> None:
    class BrokenHeaderResponse(FakeResponse):
        def header_value(self, name: str) -> str | None:
            raise Error(f"header unavailable: {name}")

    assert VisibleHhBrowser._retry_after_seconds(cast(Response, BrokenHeaderResponse())) is None


def test_chat_frame_wait_stops_after_configured_attempts(tmp_path: Path) -> None:
    page = FakePage()
    browser = make_browser(page, tmp_path)
    browser._timeout_ms = 500

    assert browser._wait_for_chat_frame(cast(Page, page)) is None


@pytest.mark.parametrize(
    ("status_qa", "expected"),
    [
        ("invitation", HhNegotiationStatus.INVITED),
        ("closed", HhNegotiationStatus.CLOSED),
        ("not-viewed", HhNegotiationStatus.APPLIED),
        ("viewed", HhNegotiationStatus.VIEWED),
    ],
)
def test_negotiation_status_covers_visible_hh_states(
    status_qa: str,
    expected: HhNegotiationStatus,
) -> None:
    assert VisibleHhBrowser._negotiation_status(status_qa, "") is expected


def test_submission_response_predicates_reject_incomplete_response() -> None:
    response = cast(Response, object())

    assert not VisibleHhBrowser._is_application_submission_response(response)
    assert not VisibleHhBrowser._is_message_submission_response(response)


def test_application_response_must_match_requested_vacancy_and_resume() -> None:
    response = FakeResponse()
    typed_response = cast(Response, response)

    assert VisibleHhBrowser._is_application_submission_response_for_target(
        typed_response,
        expected_vacancy_id="123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
    )

    response.url = "https://hh.ru/applicant/vacancy_response?vacancyId=999"
    assert not VisibleHhBrowser._is_application_submission_response_for_target(
        typed_response,
        expected_vacancy_id="123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
    )

    response.url = "https://hh.ru/applicant/vacancy_response?vacancyId=123"
    response.request.post_data = "vacancyId=999"
    assert not VisibleHhBrowser._is_application_submission_response_for_target(
        typed_response,
        expected_vacancy_id="123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
    )

    response.request.post_data = "resumeHash=another-resume"
    assert not VisibleHhBrowser._is_application_submission_response_for_target(
        typed_response,
        expected_vacancy_id="123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
    )


def test_application_confirmation_accepts_success_response_payload(tmp_path: Path) -> None:
    page = FakePage()
    page.locators["body"] = FakeLocator(text="")
    response = FakeResponse()
    response.body = '{"status":"success"}'
    browser = make_browser(page, tmp_path)

    assert browser._application_confirmation(cast(Page, page), None) == ""
    assert browser._application_confirmation(cast(Page, page), cast(Response, response)).startswith(
        "hh.ru"
    )


def test_text_helpers_handle_unavailable_page_body(tmp_path: Path) -> None:
    class BrokenTextLocator(FakeLocator):
        def inner_text(self) -> str:
            raise Error("body unavailable")

    page = FakePage()
    page.locators["body"] = BrokenTextLocator()

    assert VisibleHhBrowser._page_body_text(cast(Page, page)) == ""
    assert make_browser(page, tmp_path)._application_confirmation(cast(Page, page), None) == ""


def test_message_external_id_rejects_unusable_response_payloads() -> None:
    malformed = FakeResponse()
    malformed.body = "not-json"
    wrong_shape = FakeResponse()
    wrong_shape.body = "[]"

    assert VisibleHhBrowser._message_external_id(None) is None
    assert VisibleHhBrowser._message_external_id(cast(Response, malformed)) is None
    assert VisibleHhBrowser._message_external_id(cast(Response, wrong_shape)) is None


def test_open_applicant_form_is_noop_without_account_card(tmp_path: Path) -> None:
    page = FakePage()

    make_browser(page, tmp_path)._open_applicant_form(cast(Page, page))

    assert '[data-qa="submit-button"]' not in page.locators


def test_resume_selection_requires_expected_identity(tmp_path: Path) -> None:
    browser = make_browser(FakePage(), tmp_path)

    with pytest.raises(browser_module._ResumeSelectionError) as error:
        browser._select_exact_resume(
            browser._require_page(),
            expected_resume_hh_id="",
            expected_resume_title="Python backend",
        )

    assert not error.value.retryable


def test_resume_selection_requires_unique_selected_card(tmp_path: Path) -> None:
    page = FakePage()
    page.locators['[data-qa="resume-title"]'] = FakeLocator(0)
    browser = make_browser(page, tmp_path)

    with pytest.raises(browser_module._ResumeSelectionError) as error:
        browser._select_exact_resume(
            cast(Page, page),
            expected_resume_hh_id=TEST_RESUME_HH_ID,
            expected_resume_title="Python backend",
        )

    assert error.value.retryable


def test_resume_selection_keeps_already_selected_exact_resume(tmp_path: Path) -> None:
    page = FakePage()
    page.application_payload = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Python backend",
        "resumeHhId": TEST_RESUME_HH_ID,
        "bodyText": "Форма отклика",
    }
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    browser = make_browser(page, tmp_path)
    current = browser._application_snapshot(cast(Page, page))

    snapshot = browser._select_exact_resume(
        cast(Page, page),
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend",
        current_snapshot=current,
    )

    assert snapshot.resume_hh_id == TEST_RESUME_HH_ID
    assert snapshot.resume_title == "Python backend"
    assert page.locators['[data-qa="resume-title"]'].clicked == 0


def test_resume_selection_keeps_exact_title_when_page_hides_resume_id(
    tmp_path: Path,
) -> None:
    page = FakePage()
    page.application_payload = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Python backend",
        "resumeHhId": "",
        "bodyText": "Форма отклика",
    }
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    browser = make_browser(page, tmp_path)
    current = browser._application_snapshot(cast(Page, page))

    snapshot = browser._select_exact_resume(
        cast(Page, page),
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend",
        current_snapshot=current,
    )

    assert snapshot.resume_hh_id == ""
    assert snapshot.resume_title == "Python backend"
    assert page.locators['[data-qa="resume-title"]'].clicked == 0


def test_resume_selection_reports_card_click_failure(tmp_path: Path) -> None:
    page = FakePage()
    page.locators['[data-qa="resume-title"]'] = FakeLocator(click_error=True)
    browser = make_browser(page, tmp_path)

    with pytest.raises(browser_module._ResumeSelectionError) as error:
        browser._select_exact_resume(
            cast(Page, page),
            expected_resume_hh_id=TEST_RESUME_HH_ID,
            expected_resume_title="Python backend",
        )

    assert error.value.retryable


def test_resume_selection_stops_when_options_do_not_load(tmp_path: Path) -> None:
    page = FakePage()
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[])
    browser = make_browser(page, tmp_path)

    with pytest.raises(browser_module._ResumeSelectionError) as error:
        browser._select_exact_resume(
            cast(Page, page),
            expected_resume_hh_id=TEST_RESUME_HH_ID,
            expected_resume_title="Python backend",
        )

    assert error.value.retryable


def test_resume_selection_reopens_list_when_first_click_is_lost(tmp_path: Path) -> None:
    page = FakePage()
    payload: dict[str, object] = {
        "fields": [],
        "warnings": [],
        "resumeTitle": "Нефтяной геолог",
        "resumeHhId": "other-resume",
        "bodyText": "Форма отклика",
    }
    page.application_payload = payload
    opening_clicks = 0

    def select_target() -> None:
        payload["resumeTitle"] = "Python backend"
        payload["resumeHhId"] = TEST_RESUME_HH_ID
        page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[])

    target = FakeLocator(
        qa=f"magritte-select-option-{TEST_RESUME_HH_ID}",
        on_click=select_target,
    )

    def open_on_second_click() -> None:
        nonlocal opening_clicks
        opening_clicks += 1
        if opening_clicks == 2:
            page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[target])

    selected_card = FakeLocator(on_click=open_on_second_click)
    page.locators['[data-qa="resume-title"]'] = selected_card
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(items=[])
    page.locators[RESUME_DROPDOWN_OPTIONS_SELECTOR] = FakeLocator(items=[])
    page.locators[RESUME_GLOBAL_OPTIONS_SELECTOR] = FakeLocator(items=[])
    browser = make_browser(page, tmp_path)
    current = browser._application_snapshot(cast(Page, page))

    snapshot = browser._select_exact_resume(
        cast(Page, page),
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python backend",
        current_snapshot=current,
    )

    assert selected_card.clicked == 2
    assert selected_card.force_clicks == [False, True]
    assert target.clicked == 1
    assert snapshot.resume_hh_id == TEST_RESUME_HH_ID
    assert snapshot.resume_title == "Python backend"


def test_resume_selection_rejects_ambiguous_option_values(tmp_path: Path) -> None:
    page = FakePage()
    page.locators[RESUME_OPTIONS_SELECTOR] = FakeLocator(
        items=[
            FakeLocator(value=TEST_RESUME_HH_ID),
            FakeLocator(value=TEST_RESUME_HH_ID),
        ]
    )
    browser = make_browser(page, tmp_path)

    with pytest.raises(browser_module._ResumeSelectionError) as error:
        browser._select_exact_resume(
            cast(Page, page),
            expected_resume_hh_id=TEST_RESUME_HH_ID,
            expected_resume_title="Python backend",
        )

    assert not error.value.retryable


def test_required_payload_helpers_reject_missing_values() -> None:
    with pytest.raises(RuntimeError):
        VisibleHhBrowser._required_string({}, "title", "название")
    with pytest.raises(RuntimeError):
        VisibleHhBrowser._required_resume_text({}, "skills", "навыки")

    assert VisibleHhBrowser._resume_text({"skills": 42}, "skills") == ""


def test_relative_publication_dates_are_parsed() -> None:
    today = VisibleHhBrowser._date_time("Вакансия опубликована сегодня")
    yesterday = VisibleHhBrowser._date_time("Вакансия опубликована вчера")

    assert today is not None
    assert yesterday is not None
    assert today.astimezone().date() == datetime.now().astimezone().date()
    assert yesterday.astimezone().date() == (datetime.now().astimezone() - timedelta(days=1)).date()


def test_vacancy_availability_validates_payload() -> None:
    assert VisibleHhBrowser._vacancy_availability(None, []) is VacancyAvailability.ACTIVE
    with pytest.raises(RuntimeError):
        VisibleHhBrowser._vacancy_availability(None, {"availability": "UNKNOWN"})


def test_salary_supports_range_currency_and_net_value() -> None:
    assert VisibleHhBrowser._salary("100 000 — 150 000 € на руки") == (
        Decimal("100000"),
        Decimal("150000"),
        "EUR",
        False,
    )


def test_empty_description_has_no_sections() -> None:
    assert VisibleHhBrowser._description_sections("") == (None, None, None)


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com/resume/abc",
        "https://hh.ru/not-resume/abc",
    ],
)
def test_resume_id_rejects_external_or_malformed_link(href: str) -> None:
    with pytest.raises(RuntimeError):
        VisibleHhBrowser._resume_id(href)


def test_search_filters_serialize_boolean_and_reject_object() -> None:
    assert VisibleHhBrowser._search_filters({"only_with_salary": True}) == [
        ("only_with_salary", "true")
    ]
    with pytest.raises(ValueError):
        VisibleHhBrowser._search_filters({"salary": object()})


def test_found_vacancies_distinguishes_empty_and_malformed_result() -> None:
    assert VisibleHhBrowser._found_vacancies("", has_items=False) == 0
    with pytest.raises(RuntimeError):
        VisibleHhBrowser._found_vacancies("", has_items=True)


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com/vacancy/123",
        "https://hh.ru/not-vacancy/123",
    ],
)
def test_vacancy_url_rejects_external_or_malformed_link(href: str) -> None:
    with pytest.raises(RuntimeError):
        VisibleHhBrowser._vacancy_id_and_url(href)
