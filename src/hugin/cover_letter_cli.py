from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy import select

from hugin.adapters.yandex_ai import YandexAIClient, YandexAIError
from hugin.adapters.yandex_credentials import (
    WindowsYandexAICredentialStore,
    YandexAICredentials,
)
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import (
    ApplicationModel,
    CoverLetterModel,
    VacancyModel,
)
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.cover_letter import SYSTEM_PROMPT, CoverLetterService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Подготовка индивидуальных сопроводительных писем без отправки откликов"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser(
        "configure",
        help="сохранить настройки YandexGPT в защищенном хранилище Windows",
    )
    configure.add_argument("--folder-id", required=True, help="идентификатор каталога Yandex Cloud")
    configure.add_argument("--model", default="aliceai-llm/latest", help="облачная модель")
    subparsers.add_parser("test", help="проверить подключение к YandexGPT")

    prepare = subparsers.add_parser(
        "prepare",
        help="создать и сохранить письма для очереди, ничего не отправляя на hh.ru",
    )
    prepare.add_argument("--account-id", type=positive_int, default=1)
    prepare.add_argument("--direction", required=True, help="точное название направления")
    prepare.add_argument("--limit", type=positive_int, default=20)
    prepare.add_argument("--vacancy-id", help="подготовить письмо только для этого номера hh.ru")
    prepare.add_argument(
        "--exclude-stretch",
        action="store_true",
        help="не готовить письма для пограничных вакансий",
    )

    status = subparsers.add_parser("status", help="показать состояние подготовленных писем")
    status.add_argument("--account-id", type=positive_int, default=1)
    status.add_argument("--direction", required=True, help="точное название направления")

    show = subparsers.add_parser("show", help="показать сохраненное письмо по номеру вакансии")
    show.add_argument("--account-id", type=positive_int, default=1)
    show.add_argument("--vacancy-id", required=True, help="номер вакансии hh.ru")
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return parsed


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    store = WindowsYandexAICredentialStore()
    try:
        if arguments.command == "configure":
            api_key = getpass.getpass("Ключ Yandex AI Studio: ").strip()
            store.save(YandexAICredentials(api_key, arguments.folder_id, arguments.model))
            print("Настройки YandexGPT сохранены в защищенном хранилище Windows.")
            return 0
        if arguments.command == "test":
            client = _client(settings, store)
            response = client.complete(SYSTEM_PROMPT, "Ответь одним словом: готово")
            print(f"YandexGPT доступен: {client.model_name}: {response}")
            return 0

        upgrade_database(settings)
        database = create_database(settings)
        try:
            if arguments.command == "status":
                with database.sessions.begin() as session:
                    status = CoverLetterService(session).status(
                        account_id=arguments.account_id,
                        direction_name=arguments.direction,
                    )
                print(f"Готово: {status.ready}.")
                print(f"С ошибкой: {status.failed}.")
                print(f"Создается: {status.pending}.")
                print(f"Еще не подготовлено: {status.missing}.")
                return 0
            if arguments.command == "show":
                with database.sessions.begin() as session:
                    row = session.execute(
                        select(CoverLetterModel, VacancyModel)
                        .join(
                            ApplicationModel,
                            ApplicationModel.id == CoverLetterModel.application_id,
                        )
                        .join(VacancyModel, VacancyModel.id == CoverLetterModel.vacancy_id)
                        .where(
                            ApplicationModel.account_id == arguments.account_id,
                            VacancyModel.hh_id == arguments.vacancy_id,
                        )
                        .order_by(CoverLetterModel.id.desc())
                        .limit(1)
                    ).first()
                    if row is None:
                        raise LookupError("Письмо для этой вакансии не найдено")
                    letter, vacancy = row
                    print(f"Вакансия: {vacancy.title} (№ {vacancy.hh_id})")
                    print(f"Состояние: {letter.state.value}")
                    if letter.text:
                        print(letter.text)
                    elif letter.failure_reason:
                        print(f"Причина: {letter.failure_reason}")
                return 0

            client = _client(settings, store)
            with database.sessions.begin() as session:
                queued = ApplicationAutomationService(session).prepare_for_account_id(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    include_stretch=not arguments.exclude_stretch,
                )
                result = CoverLetterService(session, client).prepare(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    limit=arguments.limit,
                    vacancy_hh_id=arguments.vacancy_id,
                )
        finally:
            database.close()

        print(f"Новых заданий в очереди: {queued.created}. Ранее созданных: {queued.existing}.")
        labels = {
            "generated": "создано",
            "reused": "переиспользовано для связанной публикации",
            "existing": "уже готово",
            "failed": "ошибка",
        }
        for item in result.items:
            line = f"- № {item.hh_id}, {item.title}: {labels[item.action]}"
            if item.reason:
                line += f" — {item.reason}"
            print(line + ".")
        print(
            f"Создано: {result.generated}. Переиспользовано: {result.reused}. "
            f"Уже готово: {result.already_ready}. С ошибкой: {result.failed}."
        )
        print("На hh.ru ничего не отправлено.")
        return 3 if result.failed else 0
    except (LookupError, RuntimeError, ValueError, YandexAIError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПодготовка писем отменена. На hh.ru ничего не отправлено.", file=sys.stderr)
        return 130


def _client(
    settings: Settings,
    store: WindowsYandexAICredentialStore,
) -> YandexAIClient:
    environment_key = settings.yandex_ai_api_key.get_secret_value().strip()
    if environment_key:
        if not settings.yandex_ai_folder_id.strip():
            raise ValueError("Для ключа из окружения укажите HUGIN_YANDEX_AI_FOLDER_ID")
        credentials = YandexAICredentials(
            environment_key,
            settings.yandex_ai_folder_id,
            settings.yandex_ai_model,
        )
    else:
        stored_credentials = store.load()
        if stored_credentials is None:
            raise LookupError("YandexGPT не настроен; выполните hugin-letters configure")
        credentials = stored_credentials
    return YandexAIClient(
        credentials.api_key,
        credentials.folder_id,
        credentials.model,
        settings.yandex_ai_base_url,
        settings.yandex_ai_timeout_seconds,
    )


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
