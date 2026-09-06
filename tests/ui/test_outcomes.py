from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from playwright.sync_api import Route, expect, sync_playwright
from sqlalchemy import func, select

from hugin.api.app import create_app
from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import ApplicationModel, ApplicationOutcomeModel, RecruiterMessageModel
from hugin.domain.applications import ApplicationState
from hugin.domain.vacancies import VacancyData
from hugin.repositories import AccountRepository, ApplicationRepository, ResumeRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.communications import CommunicationService, RecordingMessageSender

pytestmark = pytest.mark.integration


@pytest.fixture
def local_ui(settings: Settings) -> Iterator[tuple[str, int]]:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            account = AccountRepository(session).create("Candidate")
            resume = ResumeRepository(session).upsert(account.id, "resume-ui", "Python")
            vacancy = VacancyRepository(session).upsert(
                VacancyData("ui-outcome", "Разработчик Python", "https://hh.ru/vacancy/ui-outcome")
            )
            app = ApplicationRepository(session).create_apply_intent(
                account.id, vacancy.id, resume.id
            )
            ApplicationRepository(session).transition_state(
                app.id, ApplicationState.APPLIED, {"source": "hugin_send", "hh_status": "APPLIED"}
            )
            CommunicationService(session, RecordingMessageSender()).save_incoming(
                application_id=app.id,
                hh_id="message-ui",
                body="Собеседование согласовано на 10 сентября, 14:00 по Екатеринбургу.",
            )
            CommunicationService(session, RecordingMessageSender()).save_invitation(
                application_id=app.id, hh_id="invitation-ui", title="Приглашение на собеседование"
            )
    finally:
        database.close()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        # A reset browser connection can strand Windows Proactor transports during shutdown.
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings), log_level="error", loop="asyncio:SelectorEventLoop"
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        try:
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Local UI server did not start"
            yield address, app.id
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Local UI server did not stop"


def test_record_result_reload_conflict_and_mobile(
    settings: Settings, local_ui: tuple[str, int], tmp_path: Path
) -> None:
    address, application_id = local_ui
    errors: list[str] = []
    external_requests: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1360, "height": 1000}, timezone_id="Asia/Yekaterinburg"
        )

        def route_request(route: Route) -> None:
            if not route.request.url.startswith(address + "/"):
                external_requests.append(route.request.url)
                route.abort()
            elif "/api/forms/reconcile" in route.request.url:
                route.fulfill(json=[])
            else:
                route.continue_()

        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(address)
        expect(page.get_by_role("heading", name="Результат поиска")).to_be_visible()
        expect(page.locator(".outcome-totals dd").first).to_have_text("0")
        page.get_by_role("button", name="Общение", exact=True).click()
        page.locator(".outcome-editor summary").click()
        page.get_by_label("Подтверждение приглашения на собеседование", exact=True).fill(
            "Сообщение работодателя от 5 сентября"
        )
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(page.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
        recorded = context.request.get(address + "/api/outcomes").json()
        assert recorded["confirmed_interview_invitations"] == 1
        assert recorded["scheduled_interviews"] == 0
        assert recorded["invitations"] == 0
        page.get_by_label("Согласованная дата и время").fill("2026-09-10T14:00")
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(page.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
        page.reload()
        page.get_by_role("button", name="Общение", exact=True).click()
        page.locator(".outcome-editor summary").click()
        expect(page.get_by_label("Согласованная дата и время")).to_have_value("2026-09-10T14:00")
        page.get_by_label("Подтверждение приглашения на собеседование", exact=True).fill(
            "Мой несохранённый текст"
        )
        remote = context.request.get(address + "/api/communications").json()["outcomes"][
            str(application_id)
        ]
        headers = {"X-Hugin-Session": context.request.get(address + "/api/session").json()["key"]}
        remote.pop("recorded_at")
        remote["interview_evidence"] = "Другое окно сохранило подтверждение"
        response = context.request.put(
            f"{address}/api/communications/applications/{application_id}/outcome",
            headers=headers,
            data=remote,
        )
        assert response.status == 200
        page.get_by_role("button", name="Сохранить результат", exact=True).click()
        expect(
            page.get_by_text(
                "Результат уже изменён. Обновите сведения перед сохранением.", exact=True
            )
        ).to_be_visible()
        expect(
            page.get_by_label("Подтверждение приглашения на собеседование", exact=True)
        ).to_have_value("Мой несохранённый текст")
        page.get_by_role("button", name="Обновить сведения", exact=True).click()
        page.get_by_role("button", name="Загрузить сохранённое", exact=True).click()
        expect(
            page.get_by_label("Подтверждение приглашения на собеседование", exact=True)
        ).to_have_value("Другое окно сохранило подтверждение")
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(tmp_path / "outcome-editor-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.get_by_role("button", name="Главная", exact=True).click()
        page.wait_for_function("window.scrollY === 0")
        expect(page.locator(".outcome-totals dd").first).to_have_text("1")
        page.locator(".outcome-details summary").click()
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(tmp_path / "outcome-summary-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.get_by_role("button", name="Общение", exact=True).click()
        page.get_by_role("tab", name="Приглашения").click()
        expect(page.get_by_text("Дата согласована", exact=True)).to_be_visible()
        expect(page.locator(".invitation-card strong")).to_contain_text("14:00")
        assert not errors
        assert not external_requests
        browser.close()
    database = create_database(settings)
    try:
        with database.sessions() as session:
            saved = session.scalar(
                select(ApplicationOutcomeModel).order_by(ApplicationOutcomeModel.id.desc()).limit(1)
            )
            assert saved is not None
            assert saved.interview_at is not None and saved.interview_at.hour == 9
            assert saved.interview_evidence == "Другое окно сохранило подтверждение"
            assert session.scalar(select(func.count()).select_from(ApplicationOutcomeModel)) == 3
            assert session.scalar(select(func.count()).select_from(RecruiterMessageModel)) == 1
    finally:
        database.close()


def test_record_result_for_sent_application_without_conversation(
    settings: Settings, local_ui: tuple[str, int], tmp_path: Path
) -> None:
    address, original_id = local_ui
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            original = session.get(ApplicationModel, original_id)
            assert original is not None
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "silent-ui",
                    "Разработчик сервисов автоматизации на Python без переписки",
                    "https://hh.ru/vacancy/silent-ui",
                    employer_name="Компания разработки прикладных информационных систем",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                original.account_id, vacancy.id, original.resume_id
            )
            ApplicationRepository(session).transition_state(
                application.id, ApplicationState.APPLIED, {"source": "hugin_send"}
            )
        errors: list[str] = []
        external: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda error: errors.append(str(error)))

            def local_only(route: Route) -> None:
                if not route.request.url.startswith(address + "/"):
                    external.append(route.request.url)
                    route.abort()
                elif "/api/forms/reconcile" in route.request.url:
                    route.fulfill(json=[])
                else:
                    route.continue_()

            page.route("**/*", local_only)
            page.goto(address)
            page.get_by_role("button", name="Общение", exact=True).click()
            panel = page.locator('details[aria-label="Результат любого отправленного отклика"]')
            panel.locator("summary").first.click()
            panel.get_by_label("Отправленный отклик", exact=True).select_option(str(application.id))
            panel.locator(".outcome-editor summary").click()
            panel.get_by_label("Причина, названная работодателем", exact=True).fill(
                "Вакансию уже закрыли"
            )
            panel.get_by_label("Слова работодателя и источник", exact=True).fill(
                "Работодатель сообщил по телефону"
            )
            panel.get_by_role("button", name="Сохранить результат", exact=True).click()
            expect(panel.get_by_text("Результат сохранён в Hugin.", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / "silent-outcome-mobile.png"), full_page=True)
            page.reload()
            page.get_by_role("button", name="Общение", exact=True).click()
            panel.locator("summary").first.click()
            panel.get_by_label("Отправленный отклик", exact=True).select_option(str(application.id))
            panel.locator(".outcome-editor summary").click()
            expect(
                panel.get_by_label("Причина, названная работодателем", exact=True)
            ).to_have_value("Вакансию уже закрыли")
            assert not errors
            assert not external
            browser.close()
        with database.sessions() as session:
            result = session.scalar(
                select(ApplicationOutcomeModel).where(
                    ApplicationOutcomeModel.application_id == application.id
                )
            )
            assert result is not None and result.rejection_reason == "Вакансию уже закрыли"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(RecruiterMessageModel)
                    .where(RecruiterMessageModel.application_id == application.id)
                )
                == 0
            )
    finally:
        database.close()


def test_dashboard_and_profile_remain_available_when_auxiliary_sections_fail(
    local_ui: tuple[str, int],
) -> None:
    address, _ = local_ui
    held: list[Route] = []
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/forms/reconcile?*", lambda route: held.append(route))
        page.route(
            "**/api/development",
            lambda route: route.fulfill(status=503, json={"detail": "Проверочная недоступность"}),
        )
        page.goto(address)
        expect(page.get_by_role("heading", name="Результат поиска")).to_be_visible(timeout=5000)
        expect(page.get_by_text("Часть разделов недоступна", exact=True)).to_be_visible()
        assert held
        page.get_by_role("button", name="Профиль", exact=True).click()
        expect(page.get_by_role("heading", name="Сведения о кандидате", exact=True)).to_be_visible()
        page.get_by_role("button", name="Развитие", exact=True).click()
        expect(page.get_by_text("Развитие: Проверочная недоступность", exact=True)).to_be_visible()
        page.unroute("**/api/development")
        page.reload()
        expect(page.get_by_role("heading", name="Результат поиска")).to_be_visible(timeout=5000)
        expect(page.get_by_text("Сведения загружены", exact=True)).to_be_visible()
        assert not errors
        browser.close()


@pytest.mark.parametrize("delayed_source", ["reconcile", "refresh"])
def test_delayed_form_check_does_not_replace_a_saved_answer(
    local_ui: tuple[str, int], delayed_source: str
) -> None:
    address, _ = local_ui
    held: list[Route] = []
    saved_answer = "Подтверждённый ответ кандидата"
    questions: list[dict[str, object]] = [
        {
            "field_key": key,
            "question": text,
            "field_type": "TEXT",
            "is_required": True,
            "options": [],
            "answer": None,
            "source": None,
        }
        for key, text in (("experience", "Опишите опыт"), ("other", "Уточните сведения"))
    ]
    stored = {
        "form_id": 1,
        "application_id": 1,
        "vacancy_id": "ui-form",
        "vacancy_title": "Вакансия с анкетой",
        "company": "Компания",
        "source_url": "https://hh.ru/vacancy/ui-form",
        "resume_title": "Python",
        "state": "INPUT_REQUIRED",
        "answered_count": 0,
        "unanswered_count": 2,
        "questions": questions,
    }
    stale = deepcopy(stored)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(5000)
        page.add_init_script("""(() => {
          const originalFetch = window.fetch.bind(window);
          window.formResponses = [];
          window.fetch = async (...args) => {
            const response = await originalFetch(...args);
            const path = new URL(String(args[0]), location.href).pathname;
            if (path === '/api/forms' || path === '/api/forms/reconcile') {
              window.formResponses.push(await response.clone().json());
            }
            return response;
          };
        })();""")
        page.route(
            "**/api/forms/reconcile?*",
            lambda route: (
                held.append(route) if delayed_source == "reconcile" else route.fulfill(json=[])
            ),
        )
        page.route("**/api/forms?*", lambda route: route.fulfill(json=[stored]))

        def save_answer(route: Route) -> None:
            questions[0]["answer"] = saved_answer
            questions[0]["source"] = "MANUAL"
            stored["answered_count"] = 1
            stored["unanswered_count"] = 1
            route.fulfill(json=stored)

        page.route("**/api/forms/1/answers?*", save_answer)
        page.goto(address)
        page.get_by_role("button", name="Требует внимания", exact=True).click()
        expect(page.get_by_role("heading", name="Вакансия с анкетой")).to_be_visible()
        page.get_by_text("Показать вопросы", exact=True).click()
        if delayed_source == "refresh":
            expect(page.get_by_role("button", name="Обновить данные", exact=True)).to_be_enabled()
            page.route("**/api/forms?*", lambda route: held.append(route))
            with page.expect_request("**/api/forms?*"):
                page.get_by_role("button", name="Обновить данные", exact=True).click()
        page.get_by_label("Ответ на вопрос: Опишите опыт").fill(saved_answer)
        page.locator(".form-answer-editor").first.get_by_role("button").click()
        expect(page.get_by_text(saved_answer, exact=True)).to_be_visible()
        assert held
        page.evaluate("window.formResponses = []")
        for route in held:
            route.fulfill(json=[stale])
        page.wait_for_function(
            "window.formResponses.some(forms => forms[0]?.questions[0]?.answer === null)"
        )
        page.wait_for_timeout(200)
        expect(page.get_by_text(saved_answer, exact=True)).to_be_visible()
        expect(page.get_by_label("Ответ на вопрос: Опишите опыт")).to_have_count(0)
        browser.close()
