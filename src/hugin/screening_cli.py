from __future__ import annotations

import argparse
from collections.abc import Sequence

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import get_settings
from hugin.database import create_database, upgrade_database
from hugin.domain.hh import HhFormReviewStatus
from hugin.services.hh_login import HhLoginService, LoginStatus
from hugin.services.screening_forms import ScreeningDraft, ScreeningDraftService

LOGIN_MESSAGES = {
    LoginStatus.AUTHENTICATED: "Вход в hh.ru выполнен.",
    LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные командой hugin-hh save.",
    LoginStatus.CONFIRMATION_REQUIRED: "Введите одноразовый код в открытом окне.",
    LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку в открытом окне.",
    LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль.",
    LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Черновики анкет работодателей")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("list", help="показать анкеты, ожидающие пользователя")
    show.add_argument("--account-id", type=_positive_int, default=1)

    open_form = subparsers.add_parser(
        "open",
        help="открыть свежую анкету и повторно подставить сохранённые ответы",
    )
    open_form.add_argument("--account-id", type=_positive_int, default=1)
    open_form.add_argument("--vacancy-id", required=True, help="идентификатор вакансии hh.ru")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    upgrade_database(settings)
    if arguments.command == "list":
        database = create_database(settings)
        try:
            with database.sessions.begin() as session:
                drafts = ScreeningDraftService(session).list_pending(arguments.account_id)
        finally:
            database.close()
        _print_drafts(drafts)
        return 0

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            draft = ScreeningDraftService(session).get_pending(
                arguments.account_id,
                arguments.vacancy_id,
            )
    finally:
        database.close()

    browser = VisibleHhBrowser(
        settings.browser_profile_dir(arguments.account_id),
        settings.hh_login_url,
        settings.hh_resumes_url,
        settings.hh_search_url,
        settings.hh_browser_timeout_ms,
    )
    with browser:
        login = HhLoginService(WindowsCredentialStore()).authenticate(
            arguments.account_id,
            browser,
        )
        print(LOGIN_MESSAGES[login.status])
        authenticated = login.authenticated
        if login.status in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.MANUAL_ACTION_REQUIRED,
        }:
            input("После завершения входа нажмите Enter: ")
            authenticated = browser.is_authenticated()
        if not authenticated:
            return 2

        result = browser.open_screening_form(
            draft.source_url,
            expected_resume_title=draft.resume_title,
            expected_version_hash=draft.version_hash,
            answers=draft.answers,
            cover_letter=draft.cover_letter or "",
        )
        if result.status is HhFormReviewStatus.FORM_CHANGED and result.current_form is not None:
            database = create_database(settings)
            try:
                with database.sessions.begin() as session:
                    refreshed = ScreeningDraftService(session).capture(
                        draft.application_id,
                        result.current_form,
                    )
            finally:
                database.close()
            print(
                "Работодатель изменил анкету. Черновик обновлён, старые ответы не "
                "подставлялись. Запустите открытие ещё раз."
            )
            print(
                f"Теперь заполнено надёжными данными: {len(refreshed.answers)} из "
                f"{len(refreshed.questions)}."
            )
            return 3
        if result.status is not HhFormReviewStatus.READY:
            print(_review_error(result.status, result.message))
            if result.status in {
                HhFormReviewStatus.VACANCY_CLOSED,
                HhFormReviewStatus.ALREADY_APPLIED,
            }:
                database = create_database(settings)
                try:
                    with database.sessions.begin() as session:
                        ScreeningDraftService(session).invalidate(draft.form_id)
                finally:
                    database.close()
            return 2

        print(
            f"Подставлено ответов: {len(result.filled_keys)} из {len(draft.answers)}. "
            f"Без ответа осталось: {draft.unanswered_count}."
        )
        if result.skipped_keys:
            print(
                "Не удалось безопасно подставить некоторые сохранённые ответы: "
                f"{len(result.skipped_keys)}. Проверьте их вручную."
            )
        print("Hugin не нажимал кнопку отправки.")
        input(
            "Проверьте анкету в открытом окне, дополните её и отправьте сами. "
            "После этого нажмите Enter, чтобы закрыть окно: "
        )
        return 0


def _print_drafts(drafts: tuple[ScreeningDraft, ...]) -> None:
    print(f"Анкет, ожидающих пользователя: {len(drafts)}.")
    for draft in drafts:
        print(
            f"- {draft.vacancy_id}: {draft.vacancy_title} — {draft.company}; "
            f"заполнено {len(draft.answers)} из {len(draft.questions)}, "
            f"без ответа {draft.unanswered_count}; {draft.source_url}"
        )


def _review_error(status: HhFormReviewStatus, message: str) -> str:
    messages = {
        HhFormReviewStatus.AUTH_REQUIRED: "Требуется повторный вход в hh.ru.",
        HhFormReviewStatus.CAPTCHA_REQUIRED: "Требуется пройти проверку hh.ru.",
        HhFormReviewStatus.VACANCY_CLOSED: "Вакансия закрыта.",
        HhFormReviewStatus.ALREADY_APPLIED: "Отклик уже найден на hh.ru.",
        HhFormReviewStatus.RESUME_MISMATCH: "На форме выбрано другое резюме.",
        HhFormReviewStatus.UNAVAILABLE: "Анкета сейчас недоступна.",
    }
    base = messages.get(status, "Анкету не удалось открыть.")
    return f"{base} {message}".strip()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return parsed


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
