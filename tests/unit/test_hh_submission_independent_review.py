from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.domain.hh import HhApplyResult, HhApplyStatus
from tests.unit.test_hh_browser import (
    TEST_RESUME_HH_ID,
    FakeLocator,
    FakePage,
    FakeRequest,
    FakeResponseInfo,
    FakeRoute,
    make_browser,
)


def prepared(page: FakePage, tmp_path: Path) -> VisibleHhBrowser:
    page.application_payload = {
        "questions": [],
        "warnings": [],
        "resumeTitle": "Python developer",
        "bodyText": "Application form",
    }
    page.locators['[data-qa="vacancy-response-popup-form-letter-input"]'] = FakeLocator()
    page.locators['[data-qa="vacancy-response-submit-popup"]'] = FakeLocator()
    page.locators["body"] = FakeLocator(text="Отклик отправлен")
    return make_browser(page, tmp_path)


def submit(browser: VisibleHhBrowser, guard: Callable[[], bool] = lambda: True) -> HhApplyResult:
    return browser.apply_to_vacancy(
        "https://hh.ru/vacancy/123",
        expected_resume_hh_id=TEST_RESUME_HH_ID,
        expected_resume_title="Python developer",
        cover_letter="Confirmed letter",
        submit=True,
        submit_guard=guard,
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"success":true,"error":"permission denied"}', HhApplyStatus.UNKNOWN_RESULT),
        ('{"status":"success","errors":[{"code":"invalid_resume"}]}', HhApplyStatus.UNKNOWN_RESULT),
        ('{"result":"success","error":{"message":"invalid resume"}}', HhApplyStatus.UNKNOWN_RESULT),
        ('{"success":true,"error":null,"errors":[]}', HhApplyStatus.APPLIED),
        ('{"success":true,"status":"success","result":"success"}', HhApplyStatus.APPLIED),
        ('{"message":"The response contains success:true"}', HhApplyStatus.UNKNOWN_RESULT),
    ],
)
def test_visible_success_cannot_resolve_a_conflicting_response(
    tmp_path: Path, payload: str, expected: HhApplyStatus
) -> None:
    page = FakePage("https://hh.ru/applicant/resumes")
    browser = prepared(page, tmp_path)
    page.response.body = payload
    result = submit(browser)
    assert result.status is expected
    assert page.locators['[data-qa="vacancy-response-submit-popup"]'].clicked == 1


@pytest.mark.parametrize("revoke_permission", [False, True])
@pytest.mark.parametrize("first_confirmed", [False, True])
def test_a_later_blocked_request_cannot_erase_the_result_of_an_earlier_request(
    tmp_path: Path, revoke_permission: bool, first_confirmed: bool
) -> None:
    class TwoRequestsPage(FakePage):
        first_request_allowed = False
        second_route: FakeRoute | None = None

        def expect_response(self, predicate: object, *, timeout: int) -> FakeResponseInfo:
            response = super().expect_response(predicate, timeout=timeout)
            assert self.last_route is not None and self.last_route.continued
            self.first_request_allowed = True
            request = FakeRequest()
            if not revoke_permission:
                request.post_data = "resumeHash=another-resume"
            self.second_route = FakeRoute()
            assert self.route_handler is not None
            self.route_handler(self.second_route, request)
            return response

    page = TwoRequestsPage("https://hh.ru/applicant/resumes")
    browser = prepared(page, tmp_path)
    page.response.body = '{"success":true}' if first_confirmed else "{}"
    result = submit(browser, lambda: not (revoke_permission and page.first_request_allowed))
    assert page.first_request_allowed
    assert page.second_route is not None and page.second_route.aborted
    assert not page.second_route.continued
    assert result.status is (
        HhApplyStatus.APPLIED if first_confirmed else HhApplyStatus.UNKNOWN_RESULT
    )
