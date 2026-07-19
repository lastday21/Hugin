from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Locator, Page, TimeoutError

from hugin.adapters import hh_browser as browser_module
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.services.hh_login import HhCredentials, LoginStatus


class FakeLocator:
    def __init__(
        self,
        count: int = 1,
        *,
        checked: bool = False,
        visible: bool = False,
        wait_error: bool = False,
    ) -> None:
        self._count = count
        self.checked = checked
        self.visible = visible
        self.wait_error = wait_error
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
        assert state == "visible"
        assert timeout > 0
        if self.wait_error:
            raise TimeoutError("wait")

    def all(self) -> list[FakeLocator]:
        return [self] if self._count else []

    def is_visible(self) -> bool:
        return self.visible


class FakePage:
    def __init__(self, url: str = "https://hh.ru/account/login?role=applicant") -> None:
        self.url = url
        self.locators: dict[str, FakeLocator] = {}
        self.goto_calls: list[tuple[str, str]] = []
        self.timeout: int | None = None
        self.navigation_timeout: int | None = None

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator(0))

    def goto(self, url: str, *, wait_until: str) -> None:
        self.url = url
        self.goto_calls.append((url, wait_until))

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def wait_for_timeout(self, timeout: int) -> None:
        assert timeout == 1_000


def make_browser(page: FakePage, tmp_path: Path) -> VisibleHhBrowser:
    browser = VisibleHhBrowser(tmp_path, "https://hh.ru/account/login?role=applicant", 5_000)
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
    browser = VisibleHhBrowser(tmp_path, "https://hh.ru/account/login", 5_000)

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
    browser = VisibleHhBrowser(tmp_path / "profile", "https://hh.ru/account/login", 4_000)

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
        VisibleHhBrowser(tmp_path, "https://hh.ru/account/login", 4_000).__enter__()

    assert playwright.stopped
