from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from hugin.adapters.yandex_ai import YandexAIClient, YandexAIError
from hugin.adapters.yandex_credentials import (
    WindowsYandexAICredentialStore,
    YandexAICredentials,
)
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.services.resume_improvement import (
    ResumeImprovementService,
    ResumeNarrativeBlock,
)
from hugin.services.resume_prompts import SYSTEM_PROMPT, ResumeBlockKind


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Отдельное улучшение активного ИТ-резюме")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser(
        "configure",
        help="сохранить настройки YandexGPT в защищенном хранилище Windows",
    )
    configure.add_argument("--folder-id", required=True, help="идентификатор каталога Yandex Cloud")
    configure.add_argument("--model", default="aliceai-llm/latest", help="облачная модель")

    subparsers.add_parser("test", help="проверить подключение к облачной модели")
    subparsers.add_parser("delete-config", help="удалить сохраненный ключ YandexGPT")

    run = subparsers.add_parser("run", help="создать отдельный улучшенный DOCX-черновик")
    run.add_argument("--account-id", type=positive_int, default=1)
    run.add_argument("--target-role", help="направление поиска; по умолчанию из резюме")
    run.add_argument("--vacancy-limit", type=positive_int, default=50)
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
        if arguments.command == "delete-config":
            deleted = store.delete()
            print("Настройки YandexGPT удалены." if deleted else "Сохраненных настроек нет.")
            return 0

        client = _client(settings, store)
        if arguments.command == "test":
            response = client.complete(
                SYSTEM_PROMPT,
                "Ответь одним словом: готово",
            )
            print(f"Облачная модель доступна: {client.model_name}: {response}")
            return 0

        upgrade_database(settings)
        database = create_database(settings)
        try:
            shown_blocks: set[int] = set()

            def ask(block: ResumeNarrativeBlock, question: str) -> str:
                if block.index not in shown_blocks:
                    kind = "проект" if block.kind is ResumeBlockKind.PROJECT else "место работы"
                    print(f"\nБлок {block.index}: {kind} «{block.label}»")
                    shown_blocks.add(block.index)
                print(question)
                return input("Ответ: ")

            with database.sessions.begin() as session:
                result = ResumeImprovementService(
                    session,
                    settings.data_dir,
                    client,
                ).improve(
                    arguments.account_id,
                    ask,
                    target_role=arguments.target_role,
                    vacancy_limit=arguments.vacancy_limit,
                )
        finally:
            database.close()

        print(f"\nОбработано блоков: {len(result.blocks)}.")
        print(f"Новый черновик: {result.draft_path}")
        print(f"Отчет с исходными и новыми блоками: {result.report_path}")
        print("Исходное резюме не изменено." if result.source_unchanged else "Исходник изменился.")
        print("На hh.ru ничего не опубликовано.")
        return 0
    except (LookupError, RuntimeError, ValueError, YandexAIError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nУлучшение резюме отменено.", file=sys.stderr)
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
            raise LookupError("YandexGPT не настроен; выполните hugin-resume-improve configure")
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
