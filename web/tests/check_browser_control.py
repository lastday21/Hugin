"""Плашка на подставленном DesktopBridge; HUGIN_UI_URL указывает местный Vite."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    address = os.environ.get("HUGIN_UI_URL", "http://127.0.0.1:5178")
    artifacts = Path(".work/ui/browser-control")
    artifacts.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    external: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
        page = browser.new_page(viewport={"width": 1280, "height": 750})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            if route.request.url.startswith(address + "/"):
                route.continue_()
            else:
                external.append(route.request.url)
                route.abort()

        page.route("**/*", route_request)
        page.clock.install()
        page.goto(address + "/tests/browser-control.html")
        expect(page.get_by_role("status")).to_have_count(0)
        page.evaluate("""() => {
          window.controlCalls = 0;
          window.controlValue = {state: 'requested', expires_at: null};
          window.pywebview = {api: {browser_control_status: async () => {
            window.controlCalls += 1;
            if (window.rejectControl) throw new Error('Private failure');
            if (window.holdControl) return new Promise(resolve => {
              window.resolveControl = resolve;
            });
            return window.controlValue;
          }}};
          window.dispatchEvent(new Event('pywebviewready'));
        }""")
        expect(page.get_by_text("Передача ещё не подтверждена.", exact=False)).to_be_visible()
        page.evaluate("window.controlValue = {state: 'ready', expires_at: '2026-10-21T10:00:00Z'}")
        page.clock.run_for(3_100)
        expect(page.get_by_text("Браузер передан для ручной работы", exact=True)).to_be_visible()
        page.screenshot(path=str(artifacts / "Плашка_широкая.png"))
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(artifacts / "Плашка_узкая.png"))
        page.evaluate("window.controlValue = {state: 'failed', expires_at: null}")
        page.clock.run_for(3_100)
        expect(
            page.get_by_text("Не удалось передать управление браузером", exact=True)
        ).to_be_visible()
        page.evaluate("window.rejectControl = true")
        page.clock.run_for(3_100)
        expect(page.get_by_text("Состояние передачи неизвестно.", exact=False)).to_be_visible()
        expect(page.locator("body")).not_to_contain_text("Private failure")
        page.evaluate(
            "window.rejectControl = false; "
            "window.controlValue = {state: 'automatic', expires_at: null}"
        )
        page.clock.run_for(3_100)
        expect(page.get_by_role("status")).to_have_count(0)
        calls = page.evaluate("window.controlCalls")
        page.evaluate("window.holdControl = true")
        page.clock.run_for(9_100)
        expect(page.get_by_text("Состояние передачи неизвестно.", exact=False)).to_be_visible()
        assert page.evaluate("window.controlCalls") == calls + 1
        page.evaluate("window.resolveControl({state: 'automatic', expires_at: null})")
        expect(page.get_by_role("status")).to_have_count(0)
        assert not errors, errors
        assert not external, external
        browser.close()
    result = {
        "ok": True,
        "external_requests": external,
        "page_errors": errors,
        "bridge": "substituted",
        "real_hh_checked": False,
        "mobile_width": 390,
    }
    (artifacts / "ui-private.json").write_text(json.dumps(result, indent=2), "utf8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
