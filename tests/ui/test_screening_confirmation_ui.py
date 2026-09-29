from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import Route, expect, sync_playwright
from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import (
    AnswerTemplateModel,
    ApplicationModel,
    CandidateProfileModel,
    ScreeningAnswerModel,
    ScreeningQuestionModel,
    VerifiedFactModel,
)
from hugin.domain.content import AnswerSource, ConfirmationState
from hugin.domain.hh import HhScreeningField, HhScreeningForm
from hugin.domain.vacancies import VacancyData
from hugin.repositories import ApplicationRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.screening_forms import ScreeningDraftService
from tests.ui.test_outcomes import local_ui as local_ui

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("current", [False, True])
@pytest.mark.parametrize("field_type", ["textarea", "select"])
def test_saved_answer_can_be_confirmed_while_another_question_is_unanswered(
    settings: Settings, local_ui: tuple[str, int], tmp_path: Path, current: bool, field_type: str
) -> None:
    address, existing_application_id = local_ui
    question_text = "Укажите ваши зарплатные ожидания в гросс (до вычета НДФЛ), пожалуйста."
    saved_answer = "140 000–180 000 рублей gross."
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            previous = session.get(ApplicationModel, existing_application_id)
            assert previous is not None
            vacancy = VacancyRepository(session).upsert(
                VacancyData(
                    "ui-confirm-salary",
                    "Анкета с сохранённым ответом",
                    "https://hh.ru/vacancy/ui-confirm-salary",
                )
            )
            application = ApplicationRepository(session).create_apply_intent(
                previous.account_id, vacancy.id, previous.resume_id
            )
            profile = CandidateProfileModel(
                account_id=previous.account_id,
                display_name="Кандидат",
                active_resume_id=previous.resume_id,
            )
            session.add(profile)
            session.flush()
            fact = VerifiedFactModel(
                profile_id=profile.id,
                category="screening_answer",
                content=saved_answer,
                source_type="USER",
                source_reference="screening:ui:salary",
                resume_id=previous.resume_id,
                state=ConfirmationState.CONFIRMED,
                actual_at=datetime.now(UTC) - timedelta(days=0 if current else 31),
                allow_in_forms=True,
            )
            session.add(fact)
            session.flush()
            session.add(
                AnswerTemplateModel(
                    profile_id=profile.id,
                    key="salary-ui",
                    question_pattern="Какие ваши зарплатные ожидания до вычета налогов (gross)?",
                    answer_text=saved_answer,
                    verified_fact_id=fact.id,
                )
            )
            session.flush()
            form = ScreeningDraftService(session).capture(
                application.id,
                HhScreeningForm(
                    (
                        HhScreeningField(
                            "document",
                            "Готовы предоставить выписку до собеседования?",
                            "radio",
                            is_required=True,
                            options=("Да", "Нет"),
                        ),
                        HhScreeningField(
                            "salary",
                            question_text,
                            field_type,
                            is_required=True,
                            options=(saved_answer, "Другая сумма")
                            if field_type == "select"
                            else (),
                        ),
                    )
                ),
            )
            form_id = form.form_id
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1360, "height": 1000})
            page.set_default_timeout(5000)
            external: list[str] = []

            def local_requests_only(route: Route) -> None:
                if not route.request.url.startswith(address + "/"):
                    external.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", local_requests_only)
            page.goto(address)
            page.get_by_role("button", name="Требует внимания", exact=True).click()
            card = page.locator(".form-card").filter(
                has=page.get_by_role("heading", name="Анкета с сохранённым ответом", exact=True)
            )
            card.locator("summary").click()
            expect(card.locator("p").filter(has_text=saved_answer)).to_be_visible()
            editor = card.get_by_label(f"Ответ на вопрос: {question_text}", exact=True)
            document_editor = card.get_by_label(
                "Ответ на вопрос: Готовы предоставить выписку до собеседования?", exact=True
            )
            try:
                if current:
                    expect(editor).to_have_count(0)
                else:
                    expect(editor).to_be_visible()
                    expect(editor).to_have_value(saved_answer)
                    expect(
                        card.get_by_text(
                            "Сохранённый ответ ещё не подтверждён для этой анкеты. "
                            "Проверьте его актуальность и условия вопроса.",
                            exact=True,
                        )
                    ).to_be_visible()
                    salary_item = card.locator("li").filter(
                        has=page.get_by_text(question_text, exact=True)
                    )
                    document_editor.select_option("Нет")
                    held: list[Route] = []
                    page.route(
                        f"**/api/forms/{form_id}/answers?*", lambda route: held.append(route)
                    )
                    with page.expect_request(f"**/api/forms/{form_id}/answers?*"):
                        salary_item.get_by_role(
                            "button", name="Сохранить ответ", exact=True
                        ).click()
                    try:
                        expect(editor).to_be_disabled()
                        expect(document_editor).to_be_enabled()
                        assert len(held) == 1
                    finally:
                        for route in held:
                            route.continue_()
                    expect(editor).to_have_count(0)
                    expect(document_editor).to_have_value("Нет")
                expect(document_editor).to_be_visible()
                assert not external
            finally:
                page.screenshot(path=str(tmp_path / "form-confirmation.png"), full_page=True)
                browser.close()
        with database.sessions() as session:
            answer = session.scalar(
                select(ScreeningAnswerModel)
                .join(ScreeningQuestionModel)
                .where(
                    ScreeningQuestionModel.form_id == form_id,
                    ScreeningQuestionModel.field_key == "salary",
                )
            )
            assert answer is not None and answer.is_confirmed
            assert answer.source is (AnswerSource.BANK if current else AnswerSource.USER)
            document = session.scalar(
                select(ScreeningAnswerModel)
                .join(ScreeningQuestionModel)
                .where(
                    ScreeningQuestionModel.form_id == form_id,
                    ScreeningQuestionModel.field_key == "document",
                )
            )
            assert document is not None and document.answer_text is None
    finally:
        database.close()
