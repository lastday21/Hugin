from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from tests.ui.test_outcomes import local_ui as local_ui

pytestmark = pytest.mark.integration


def test_invitation_count_survives_rescheduling_and_can_be_corrected_on_mobile(
    local_ui: tuple[str, int], tmp_path: Path
) -> None:
    address, _ = local_ui
    errors: list[str] = []
    external: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, timezone_id="Asia/Yekaterinburg"
        )
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route: Route) -> None:
            if not route.request.url.startswith(address + "/"):
                external.append(route.request.url)
                route.abort()
            elif "/api/forms/reconcile" in route.request.url:
                route.fulfill(json=[])
            else:
                route.continue_()

        page.route("**/*", route_request)
        page.goto(address)

        def edit() -> None:
            page.get_by_role("button", name="Общение", exact=True).click()
            page.get_by_role("tab", name="Приглашения").click()
            page.locator(".invitation-card .outcome-editor summary").click()

        def check_totals(invited: str, dated: str) -> None:
            page.get_by_role("button", name="Главная", exact=True).click()
            totals = page.locator(".outcome-totals dd")
            expect(totals.nth(0)).to_have_text(invited)
            expect(totals.nth(1)).to_have_text(dated)
            expect(totals.nth(2)).to_have_text("0")
            expect(totals.nth(3)).to_have_text("1")

        edit()
        expect(page.get_by_label("Согласованная дата и время")).to_have_value("")
        page.get_by_label("Подтверждение приглашения на собеседование", exact=True).fill(
            "Работодатель предложил собеседование по телефону; дату ещё выбираем."
        )
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(page.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
        expect(page.locator(".invitation-card .outcome-editor summary")).to_have_text(
            "Результат общения · приглашение подтверждено"
        )
        check_totals("1", "0")
        edit()
        page.get_by_label("Согласованная дата и время").fill("2026-09-10T14:00")
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(page.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
        check_totals("1", "1")
        edit()
        page.get_by_label("Согласованная дата и время").fill("")
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(page.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
        check_totals("1", "0")
        page.screenshot(
            path=str(tmp_path / "confirmed-invitation-without-date.png"), full_page=True
        )
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        edit()
        page.get_by_label("Подтверждение приглашения на собеседование", exact=True).fill("")
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(page.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
        page.reload()
        check_totals("0", "0")
        assert not errors
        assert not external
        browser.close()
