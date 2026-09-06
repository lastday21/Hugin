from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from tests.unit.test_application_worker import FakeApplicationService, fake_job, prepare_worker
from tests.unit.test_application_worker import setup_function as reset_fake_service
from tests.unit.test_hh_browser import TEST_RESUME_HH_ID, FakeLocator, FakePage, make_browser


def _prepared_browser(tmp_path: Path) -> tuple[VisibleHhBrowser, FakePage, FakeLocator]:
    page = FakePage("https://hh.ru/applicant/resumes")
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python developer",
        "bodyText": "Application form",
    }
    submit = FakeLocator()
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    page.locators["body"] = FakeLocator(text="Application form")
    page.response.body = "{}"
    return make_browser(page, tmp_path), page, submit


def _submit(browser: VisibleHhBrowser, guard: Callable[[], bool] = lambda: True) -> HhApplyResult:
    return browser.apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python developer",
        cover_letter="Confirmed letter",
        submit=True,
        submit_guard=guard,
    )


@pytest.mark.parametrize(
    "body",
    [
        "После нажатия появится сообщение «Отклик отправлен».",
        "Не удалось подтвердить, что отклик отправлен.",  # noqa: RUF001
        "Вы откликнулись на другую вакансию.",
    ],
)
def test_unrelated_success_words_do_not_confirm_the_target_application(
    tmp_path: Path, body: str
) -> None:
    browser, page, submit = _prepared_browser(tmp_path)
    page.locators["body"].text = body

    result = _submit(browser)

    assert result.status is HhApplyStatus.UNKNOWN_RESULT
    assert submit.clicked == 1
    assert page.last_route is not None and page.last_route.continued


@pytest.mark.parametrize(
    "body",
    [
        '{"success":false,"analytics":{"success":true}}',
        '{"status":"error","cached":{"status":"success"}}',
        '{"result":"error","previous":{"result":"success"}}',
        '{"success":true,"status":"error"}',
        '{"success":false,"result":"success"}',
        '{"analytics":{"success":true}}',
        '[{"success":true}]',
        'prefix {"success":true}',
        '{"success":"true"}',
    ],
)
def test_nested_success_does_not_override_a_failed_submission_result(
    tmp_path: Path, body: str
) -> None:
    browser, page, submit = _prepared_browser(tmp_path)
    page.response.body = body

    result = _submit(browser)

    assert result.status is HhApplyStatus.UNKNOWN_RESULT
    assert submit.clicked == 1


def test_success_on_another_vacancy_page_is_not_target_confirmation(tmp_path: Path) -> None:
    browser, page, submit = _prepared_browser(tmp_path)

    def move_to_other_vacancy() -> None:
        page.url = "https://hh.ru/vacancy/999"
        page.locators["body"].text = "Отклик отправлен"

    submit.on_click = move_to_other_vacancy
    result = _submit(browser)

    assert result.status is HhApplyStatus.UNKNOWN_RESULT
    assert submit.clicked == 1


@pytest.mark.parametrize("guard_raises", [False, True])
def test_revoked_permission_after_button_readiness_blocks_the_request(
    tmp_path: Path, guard_raises: bool
) -> None:
    browser, page, _ = _prepared_browser(tmp_path)
    ready = True
    guard_calls = 0

    class RevokingButton(FakeLocator):
        def click(
            self,
            *,
            force: bool = False,
            no_wait_after: bool = False,
            timeout: int | None = None,
            trial: bool = False,
        ) -> None:
            nonlocal ready
            super().click(force=force, no_wait_after=no_wait_after, timeout=timeout, trial=trial)
            if trial:
                ready = False

    def guard() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        if not ready and guard_raises:
            raise RuntimeError("Saved letter changed")
        return ready

    page.locators['[data-qa="vacancy-response-submit-popup"]'] = RevokingButton()
    page.response.body = '{"success":true}'
    result = _submit(browser, guard)

    assert result.status in {HhApplyStatus.MANUAL_REVIEW_REQUIRED, HhApplyStatus.RETRYABLE_ERROR}
    assert guard_calls >= 2
    assert page.last_route is None or not page.last_route.continued


@pytest.mark.parametrize(
    "body",
    ['{"success":true}', '{"status":"success"}', '{"result":"success"}'],
)
def test_matching_request_with_explicit_success_remains_confirmed(
    tmp_path: Path, body: str
) -> None:
    browser, page, submit = _prepared_browser(tmp_path)
    page.response.body = body

    result = _submit(browser)

    assert result.status is HhApplyStatus.APPLIED
    assert submit.clicked == 1


@pytest.mark.parametrize("guard_raises", [False, True])
def test_permission_revoked_at_the_request_boundary_never_leaves_the_browser(
    tmp_path: Path, guard_raises: bool
) -> None:
    browser, page, submit = _prepared_browser(tmp_path)
    guard_calls = 0

    def guard() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls < 3:
            return True
        if guard_raises:
            raise RuntimeError("The queue permission is unavailable")
        return False

    page.response.body = '{"success":true}'
    result = _submit(browser, guard)

    assert result.status is (
        HhApplyStatus.RETRYABLE_ERROR if guard_raises else HhApplyStatus.MANUAL_REVIEW_REQUIRED
    )
    assert guard_calls == 3
    assert submit.clicked <= 1
    assert page.last_route is not None and page.last_route.aborted
    assert not page.last_route.continued


def test_worker_stop_during_button_trial_revokes_the_actual_browser_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reset_fake_service()
    worker = prepare_worker(monkeypatch, tmp_path)
    browser, page, _ = _prepared_browser(tmp_path)
    job = fake_job("123")
    values = cast(SimpleNamespace, job)
    values.resume.hh_id = TEST_RESUME_HH_ID
    values.resume.title = "Python developer"
    values.cover_letter = "Confirmed letter"
    values.cover_letter_id = 15
    values.cover_letter_sha256 = sha256(values.cover_letter.encode()).hexdigest()

    class StoppingButton(FakeLocator):
        def click(
            self,
            *,
            force: bool = False,
            no_wait_after: bool = False,
            timeout: int | None = None,
            trial: bool = False,
        ) -> None:
            super().click(force=force, no_wait_after=no_wait_after, timeout=timeout, trial=trial)
            if trial:
                worker.stop()

    submit = StoppingButton()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = submit
    try:
        result = _submit(browser, lambda: worker._background_submission_is_allowed(job))
        assert FakeApplicationService.enabled
        assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
        assert submit.clicked == 0
        assert page.last_route is None
        assert FakeApplicationService.submission_checks == [
            {
                "task_id": job.task.id,
                "letter_id": job.cover_letter_id,
                "letter_sha256": job.cover_letter_sha256,
                "resume_hh_id": TEST_RESUME_HH_ID,
                "resume_title": "Python developer",
            }
        ]
    finally:
        reset_fake_service()


@pytest.mark.parametrize(
    "body",
    [
        '{"vacancyId":"999","resumeHash":"resume-hash"}',
        '{"vacancyId":"123","resumeHash":"other-resume"}',
    ],
)
def test_changed_target_request_is_blocked_even_with_valid_page(tmp_path: Path, body: str) -> None:
    browser, page, _ = _prepared_browser(tmp_path)
    page.response.request.post_data = body
    page.response.body = '{"success":true}'

    result = _submit(browser)

    assert result.status is HhApplyStatus.MANUAL_REVIEW_REQUIRED
    assert page.last_route is not None and page.last_route.aborted
    assert not page.last_route.continued
