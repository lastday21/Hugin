from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from hugin import screening_cli
from hugin.domain import AnswerSource, ScreeningFormState
from hugin.domain.hh import (
    HhFormReviewResult,
    HhFormReviewStatus,
    HhScreeningField,
    HhScreeningForm,
)
from hugin.services.hh_login import LoginStatus
from hugin.services.screening_forms import ScreeningDraft, ScreeningDraftQuestion


def make_draft(*, answers: bool = True) -> ScreeningDraft:
    return ScreeningDraft(
        form_id=10,
        application_id=20,
        vacancy_id="123",
        vacancy_title="Python разработчик",
        company="Компания",
        source_url="https://hh.ru/vacancy/123",
        resume_title="Python",
        version_hash="version-1",
        state=ScreeningFormState.INPUT_REQUIRED,
        questions=(
            ScreeningDraftQuestion(
                field_key="0:name:telegram",
                question="Укажите Telegram",
                field_type="text",
                is_required=True,
                options=(),
                answer="@ivan" if answers else None,
                source=AnswerSource.PROFILE if answers else None,
            ),
            ScreeningDraftQuestion(
                field_key="1:name:motivation",
                question="Почему вы хотите работать у нас?",
                field_type="textarea",
                is_required=True,
                options=(),
                answer=None,
                source=None,
            ),
        ),
        cover_letter="Здравствуйте!",
    )


class FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


class FakeSessions:
    def begin(self) -> FakeTransaction:
        return FakeTransaction()


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = FakeSessions()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSettings:
    hh_login_url = "https://hh.ru/account/login"
    hh_resumes_url = "https://hh.ru/applicant/resumes"
    hh_search_url = "https://hh.ru/search/vacancy"
    hh_browser_timeout_ms = 5_000

    @staticmethod
    def browser_profile_dir(account_id: int) -> Path:
        return Path(f"profile-{account_id}")


class FakeDraftService:
    draft: ClassVar[ScreeningDraft] = make_draft()
    captured: ClassVar[list[tuple[int, HhScreeningForm]]] = []
    invalidated: ClassVar[list[int]] = []

    def __init__(self, session: object) -> None:
        assert session is not None

    def list_pending(self, account_id: int) -> tuple[ScreeningDraft, ...]:
        assert account_id == 1
        return (self.draft,)

    def get_pending(self, account_id: int, vacancy_id: str) -> ScreeningDraft:
        assert account_id == 1
        assert vacancy_id == "123"
        return self.draft

    def capture(self, application_id: int, form: HhScreeningForm) -> ScreeningDraft:
        self.captured.append((application_id, form))
        return self.draft

    def invalidate(self, form_id: int) -> None:
        self.invalidated.append(form_id)


class FakeLoginService:
    status: ClassVar[LoginStatus] = LoginStatus.AUTHENTICATED

    def __init__(self, store: object) -> None:
        assert store is not None

    def authenticate(self, account_id: int, browser: object) -> SimpleNamespace:
        assert account_id == 1
        assert browser is not None
        return SimpleNamespace(
            status=self.status,
            authenticated=self.status is LoginStatus.AUTHENTICATED,
        )


class FakeBrowser:
    result: ClassVar[HhFormReviewResult] = HhFormReviewResult(
        HhFormReviewStatus.READY,
        "https://hh.ru/applicant/vacancy_response?vacancyId=123",
        filled_keys=("0:name:telegram",),
    )
    open_arguments: ClassVar[dict[str, object] | None] = None
    authenticated: ClassVar[bool] = True

    def __init__(self, *args: object) -> None:
        assert args

    def __enter__(self) -> FakeBrowser:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def is_authenticated(self) -> bool:
        return self.authenticated

    def open_screening_form(self, source_url: str, **kwargs: object) -> HhFormReviewResult:
        type(self).open_arguments = {"source_url": source_url, **kwargs}
        return self.result


@pytest.fixture(autouse=True)
def fake_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDraftService.draft = make_draft()
    FakeDraftService.captured = []
    FakeDraftService.invalidated = []
    FakeLoginService.status = LoginStatus.AUTHENTICATED
    FakeBrowser.result = HhFormReviewResult(
        HhFormReviewStatus.READY,
        "https://hh.ru/applicant/vacancy_response?vacancyId=123",
        filled_keys=("0:name:telegram",),
    )
    FakeBrowser.open_arguments = None
    FakeBrowser.authenticated = True
    monkeypatch.setattr(screening_cli, "get_settings", FakeSettings)
    monkeypatch.setattr(screening_cli, "upgrade_database", lambda settings: None)
    monkeypatch.setattr(screening_cli, "create_database", lambda settings: FakeDatabase())
    monkeypatch.setattr(screening_cli, "ScreeningDraftService", FakeDraftService)
    monkeypatch.setattr(screening_cli, "HhLoginService", FakeLoginService)
    monkeypatch.setattr(screening_cli, "VisibleHhBrowser", FakeBrowser)
    monkeypatch.setattr(screening_cli, "WindowsCredentialStore", object)


def test_list_prints_pending_drafts(capsys: pytest.CaptureFixture[str]) -> None:
    result = screening_cli.run(["list", "--account-id", "1"])

    assert result == 0
    output = capsys.readouterr().out
    assert "Анкет, ожидающих пользователя: 1" in output
    assert "заполнено 1 из 2" in output
    assert "https://hh.ru/vacancy/123" in output


def test_open_refills_answers_and_waits_for_manual_submit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def answer_input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", answer_input)

    result = screening_cli.run(["open", "--vacancy-id", "123"])

    assert result == 0
    assert FakeBrowser.open_arguments == {
        "source_url": "https://hh.ru/vacancy/123",
        "expected_resume_title": "Python",
        "expected_version_hash": "version-1",
        "answers": {"0:name:telegram": "@ivan"},
        "cover_letter": "Здравствуйте!",
    }
    assert "Hugin не нажимал кнопку отправки" in capsys.readouterr().out
    assert len(prompts) == 1


def test_changed_form_replaces_draft_without_refilling(
    capsys: pytest.CaptureFixture[str],
) -> None:
    current = HhScreeningForm(
        fields=(HhScreeningField("0:name:new", "Новый вопрос", "text", True),)
    )
    FakeBrowser.result = HhFormReviewResult(
        HhFormReviewStatus.FORM_CHANGED,
        "https://hh.ru/applicant/vacancy_response?vacancyId=123",
        current_form=current,
    )

    result = screening_cli.run(["open", "--vacancy-id", "123"])

    assert result == 3
    assert FakeDraftService.captured == [(20, current)]
    assert "старые ответы не подставлялись" in capsys.readouterr().out


def test_closed_vacancy_invalidates_draft(capsys: pytest.CaptureFixture[str]) -> None:
    FakeBrowser.result = HhFormReviewResult(
        HhFormReviewStatus.VACANCY_CLOSED,
        "https://hh.ru/vacancy/123",
    )

    result = screening_cli.run(["open", "--vacancy-id", "123"])

    assert result == 2
    assert FakeDraftService.invalidated == [10]
    assert "Вакансия закрыта" in capsys.readouterr().out


def test_manual_login_can_continue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeLoginService.status = LoginStatus.MANUAL_ACTION_REQUIRED
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    result = screening_cli.run(["open", "--vacancy-id", "123"])

    assert result == 0
    assert "Завершите вход" in capsys.readouterr().out


def test_missing_credentials_stops_before_open(capsys: pytest.CaptureFixture[str]) -> None:
    FakeLoginService.status = LoginStatus.CREDENTIALS_REQUIRED

    result = screening_cli.run(["open", "--vacancy-id", "123"])

    assert result == 2
    assert FakeBrowser.open_arguments is None
    assert "Сначала сохраните данные" in capsys.readouterr().out


def test_positive_identifier_validation() -> None:
    with pytest.raises(SystemExit):
        screening_cli.run(["list", "--account-id", "0"])
