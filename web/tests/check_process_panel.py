"""Проверка панели на подставленных ответах API; работающий Vite указан в HUGIN_UI_URL."""

import json
import os

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    keys = ["search", "evaluation", "applications", "synchronization", "replies"]
    names = ["Поиск и загрузка", "Оценка вакансий", "Отклики", "Переписка и статусы", "Ответы работодателям"]
    state = {
        "processes": [
            {"key": key, "name": name, "enabled": False, "state": "disabled",
             "reason": "Выключено пользователем", "last_started_at": None,
             "last_finished_at": None, "heartbeat_at": None, "runs": 0, "completed": 0}
            for key, name in zip(keys, names, strict=True)
        ],
        "synchronization": {"message_interval_minutes": 10, "status_interval_minutes": 60, "check_now_pending": False},
        "funnel": {"total": 8, "scope": "Уникальные вакансии активных направлений.", "stages": [
            {"key": "awaiting_details", "name": "Ожидают описания", "count": 5},
            {"key": "ready", "name": "Готовы к отклику", "count": 3},
        ]},
        "last_search": {"observed_at": "2026-09-09T10:00:00Z", "query": "Python", "region": "1",
                        "page": 1, "found": None, "coverage_exhausted": None,
                        "coverage_page_limit": 3, "job_key": "search:1"},
    }
    writes = []
    reject_next = False
    fail_read = False
    errors = []

    def respond(route):
        nonlocal reject_next
        request = route.request
        path = request.url.split("/api", 1)[1].split("?", 1)[0]
        if path == "/session":
            route.fulfill(json={"key": "isolated-ui-test"})
            return
        if path == "/autonomy" and request.method == "PUT":
            payload = request.post_data_json
            assert "auto_prepare_replies" not in payload
            assert "auto_send_approved_replies" not in payload
            writes.append((path, payload))
            route.fulfill(json={**payload, "auto_prepare_replies": False, "auto_send_approved_replies": False})
            return
        if path.startswith("/processes"):
            if request.method != "GET":
                payload = request.post_data_json
                writes.append((path, payload))
                assert request.headers.get("x-hugin-session") == "isolated-ui-test"
                if reject_next:
                    reject_next = False
                    route.fulfill(status=409, json={"detail": "Проверка hh.ru требует действия пользователя"})
                    return
                if path.endswith("stop-all"):
                    for item in state["processes"]:
                        item.update(enabled=False, state="disabled")
                    state["synchronization"]["check_now_pending"] = False
                elif path.endswith("check-now"):
                    state["synchronization"]["check_now_pending"] = True
                elif path.endswith("schedule"):
                    state["synchronization"].update(payload)
                else:
                    key = path.rsplit("/", 1)[1]
                    next(item for item in state["processes"] if item["key"] == key).update(payload)
            elif fail_read:
                route.fulfill(status=503, json={"detail": "Недоступно"})
                return
            route.fulfill(json=state)
            return
        route.abort()
        raise AssertionError(f"Неожиданный путь API: {path}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/**", respond)
        page.goto(os.environ.get("HUGIN_UI_URL", "http://127.0.0.1:5176") + "/tests/process-panel.html")
        expect(page.get_by_role("switch")).to_have_count(5)
        expect(page.get_by_role("heading", name="Найденные вакансии: 8")).to_be_visible()
        for index, name in enumerate(names):
            page.get_by_role("switch", name=name, exact=True).click()
            expect(page.get_by_role("switch", name=name, exact=True)).to_be_checked()
            assert writes[-1] == (f"/processes/{keys[index]}", {"enabled": True})
            for later in names[index + 1:]:
                expect(page.get_by_role("switch", name=later, exact=True)).not_to_be_checked()
        page.get_by_role("button", name="Остановить всё").click()
        for name in names:
            expect(page.get_by_role("switch", name=name, exact=True)).not_to_be_checked()
        page.get_by_role("button", name="Проверить сейчас", exact=True).click()
        expect(page.get_by_role("button", name="Проверка ожидает своей очереди")).to_be_disabled()
        expect(page.get_by_role("switch", name="Переписка и статусы", exact=True)).not_to_be_checked()
        messages = page.get_by_label("Сообщения, каждые (минуты)")
        messages.fill("0")
        expect(page.get_by_role("button", name="Сохранить частоту")).to_be_disabled()
        messages.fill("25")
        page.get_by_label("Статусы откликов, каждые (минуты)").fill("120")
        page.get_by_role("button", name="Сохранить частоту").click()
        expect(page.get_by_role("status")).to_have_text("Частота проверок сохранена")
        assert state["synchronization"]["message_interval_minutes"] == 25
        assert state["synchronization"]["status_interval_minutes"] == 120
        page.get_by_text("Последнее чтение выдачи hh.ru", exact=True).click()
        expect(page.locator(".process-search dd").filter(has_text="Неизвестно")).to_have_count(1)
        reject_next = True
        page.get_by_role("switch", name="Отклики", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("требует действия пользователя")
        expect(page.get_by_role("switch", name="Отклики", exact=True)).not_to_be_checked()
        messages.fill("35")
        page.get_by_role("button", name="Остановить всё").click()
        expect(messages).to_have_value("35")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot = os.environ.get("HUGIN_UI_SCREENSHOT")
        if screenshot:
            page.screenshot(path=screenshot, full_page=True)
        fail_read = True
        page.get_by_role("button", name="Остановить всё").click()
        expect(page.get_by_role("alert")).to_contain_text("Не удалось обновить состояние")
        expect(page.get_by_role("switch", name="Поиск и загрузка", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Остановить всё")).to_be_enabled()
        saved = page.evaluate("""async () => {
            const {updateAutonomyPolicy} = await import('/src/api.ts');
            return updateAutonomyPolicy({auto_prepare_replies: true, auto_send_approved_replies: true,
                auto_apply_stretch: true, auto_submit_simple_forms: true, auto_reconcile_unknown: true,
                reuse_confirmed_profile_facts: true, mark_opened_invitations_seen: true,
                mutable_fact_validity_days: 90, reply_templates: []});
        }""")
        assert not saved["auto_prepare_replies"] and not saved["auto_send_approved_replies"]
        assert not errors, errors
        browser.close()
    print(json.dumps({"result": "passed", "writes": len(writes), "checks": [
        "five independent switches", "stop all", "one-shot without enabling recurring",
        "interval validation and save", "unknown external count", "409 preserves disabled state",
        "unsaved interval survives refresh", "desktop and narrow viewport", "stale state disables start",
        "settings cannot restore stale reply switches",
    ]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
