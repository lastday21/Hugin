from __future__ import annotations

from pathlib import Path
from types import TracebackType
from urllib.parse import urlparse

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

from hugin.services.hh_login import HhCredentials, LoginStatus


class VisibleHhBrowser:
    def __init__(self, profile_dir: Path, login_url: str, timeout_ms: int) -> None:
        self._profile_dir = profile_dir
        self._login_url = login_url
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
