from copy import deepcopy

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from tests.ui.test_outcomes import local_ui as local_ui

pytestmark = pytest.mark.integration


def test_reapplication_review_preserves_reason_when_sources_change(
    local_ui: tuple[str, int],
) -> None:
    address, _application_id = local_ui
    queue = [
        {
            "task_id": 999,
            "vacancy_id": "review-ui",
            "title": "Python developer",
            "company": "Employer",
            "region": "Казань",
            "source_url": "https://hh.ru/vacancy/review-ui",
            "resume_title": "Python",
            "direction": "Python backend",
            "state": "REVIEW_REQUIRED",
            "priority": 90,
            "fit_tier": 1,
            "fit_reason": "Подтверждённые навыки",
            "scheduled_at": "2026-10-10T10:00:00Z",
            "last_error": "Требования изменились",
            "letter_state": "READY",
            "form_state": None,
            "reapplication_review_available": True,
        }
    ]
    preview = {
        "allowed": False,
        "code": "REPOST_CHANGED",
        "reason": "Полный текст роли изменился.",
        "recheck_at": None,
        "review_key": "a" * 64,
        "can_review": True,
        "previous": [
            {
                "application_id": 10,
                "title": "Python developer",
                "source_url": "https://hh.ru/vacancy/old-ui",
            }
        ],
    }
    posted: list[dict[str, object]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1360, "height": 1000})

        def intercept(route: Route) -> None:
            url = route.request.url
            if not url.startswith(address + "/"):
                route.abort()
            elif "/api/forms/reconcile" in url:
                route.fulfill(json=[])
            elif "/reapplication-review" in url:
                if route.request.method == "GET":
                    data = deepcopy(preview)
                    data["review_key"] = "b" * 64 if posted else "a" * 64
                    route.fulfill(json=data)
                else:
                    payload = route.request.post_data_json
                    assert isinstance(payload, dict)
                    posted.append(payload)
                    if len(posted) == 1:
                        route.fulfill(
                            status=409, json={"detail": "Источники изменились. Обновите разбор."}
                        )
                    else:
                        route.fulfill(
                            json={
                                **preview,
                                "allowed": True,
                                "can_review": False,
                                "review_key": None,
                            }
                        )
            elif "/api/queue?" in url:
                route.fulfill(json=queue)
            else:
                route.continue_()

        context.route("**/*", intercept)
        page = context.new_page()
        page.goto(address)
        page.get_by_role("button", name="Вакансии", exact=True).click()
        page.get_by_role("button", name="Разобрать повтор", exact=True).click()
        field = page.get_by_label("Обоснование повторного отклика", exact=True)
        field.fill("да")
        save = page.get_by_role("button", name="Сохранить обоснование", exact=True)
        expect(save).to_be_disabled()
        reason = (
            "Проверил новые требования и подтверждённый опыт: появилось подходящее направление."
        )
        field.fill(reason)
        page.reload()
        page.get_by_role("button", name="Вакансии", exact=True).click()
        page.get_by_role("button", name="Разобрать повтор", exact=True).click()
        expect(field).to_have_value(reason)
        save.click()
        expect(
            page.get_by_text("Источники изменились. Обновите разбор.", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Обновить разбор", exact=True).click()
        expect(field).to_have_value(reason)
        page.set_viewport_size({"width": 390, "height": 844})
        save.click()
        expect(
            page.get_by_text(
                "Обоснование сохранено. Очередь проверит отклик перед продолжением.", exact=True
            )
        ).to_be_visible()
        assert posted == [
            {"reason": reason, "review_key": "a" * 64},
            {"reason": reason, "review_key": "b" * 64},
        ]
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()
