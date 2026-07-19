from __future__ import annotations

import argparse
import getpass
from collections.abc import Sequence

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import get_settings
from hugin.services.hh_login import HhCredentials, HhLoginService, LoginStatus

STATUS_MESSAGES = {
    LoginStatus.AUTHENTICATED: "Вход в hh.ru выполнен.",
    LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные командой hugin-hh save.",
    LoginStatus.CONFIRMATION_REQUIRED: "Введите одноразовый код в открытом окне.",
    LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку в открытом окне.",
    LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль.",
    LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Вход в профиль соискателя hh.ru")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("save", "сохранить логин и пароль в защищённом хранилище Windows"),
        ("login", "открыть видимый браузер и войти"),
        ("delete", "удалить сохранённые данные"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--account-id", type=positive_int, default=1)
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return parsed


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    store = WindowsCredentialStore()

    if arguments.command == "save":
        login = input("Почта или телефон hh.ru: ").strip()
        password = getpass.getpass("Пароль hh.ru: ")
        store.save(arguments.account_id, HhCredentials(login=login, password=password))
        print("Данные сохранены в защищённом хранилище Windows.")
        return 0

    if arguments.command == "delete":
        deleted = store.delete(arguments.account_id)
        print("Данные удалены." if deleted else "Сохранённых данных не найдено.")
        return 0

    settings = get_settings()
    browser = VisibleHhBrowser(
        settings.browser_profile_dir(arguments.account_id),
        settings.hh_login_url,
        settings.hh_browser_timeout_ms,
    )
    with browser:
        result = HhLoginService(store).authenticate(arguments.account_id, browser)
        print(STATUS_MESSAGES[result.status])
        if result.status in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.MANUAL_ACTION_REQUIRED,
        }:
            input("После завершения входа нажмите Enter: ")
            if browser.is_authenticated():
                print(STATUS_MESSAGES[LoginStatus.AUTHENTICATED])
                return 0
        return 0 if result.authenticated else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
