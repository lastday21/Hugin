from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from hugin.adapters.resume_documents import ResumeDocumentError, ResumeDocumentReader
from hugin.core.settings import get_settings
from hugin.database import create_database, upgrade_database
from hugin.services.resume_profile import (
    ProfileFactService,
    ProfileQuestionService,
    ResumeImportService,
    ResumeProfileExtractor,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Импорт и проверка резюме Hugin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="проверить чтение резюме без записи в базу")
    inspect.add_argument("file", type=Path, help="путь к PDF или DOCX")

    import_resume = subparsers.add_parser("import", help="импортировать активное ИТ-резюме")
    import_resume.add_argument("file", type=Path, help="путь к PDF или DOCX")
    import_resume.add_argument("--account-id", type=positive_int, default=1)
    import_resume.add_argument(
        "--hh-resume-id",
        help="идентификатор резюме после синхронизации с hh.ru",
    )

    facts = subparsers.add_parser("facts", help="показать факты, ожидающие подтверждения")
    facts.add_argument("--account-id", type=positive_int, default=1)

    confirm_fact = subparsers.add_parser("confirm-fact", help="подтвердить факт резюме")
    confirm_fact.add_argument("--account-id", type=positive_int, default=1)
    confirm_fact.add_argument("--fact-id", type=positive_int, required=True)

    reject_fact = subparsers.add_parser("reject-fact", help="отклонить факт резюме")
    reject_fact.add_argument("--account-id", type=positive_int, default=1)
    reject_fact.add_argument("--fact-id", type=positive_int, required=True)

    questions = subparsers.add_parser("questions", help="показать вопросы без ответа")
    questions.add_argument("--account-id", type=positive_int, default=1)

    answer = subparsers.add_parser("answer", help="сохранить подтверждённый ответ")
    answer.add_argument("--account-id", type=positive_int, default=1)
    answer.add_argument("--key", required=True, help="ключ вопроса")
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return parsed


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "inspect":
        try:
            document = ResumeDocumentReader().read(arguments.file)
            profile = ResumeProfileExtractor().extract(document)
        except (FileNotFoundError, ResumeDocumentError) as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            return 2
        pages = document.page_count if document.page_count is not None else "не определено"
        print(f"Формат: {document.source_type.value}.")
        print(f"Страниц: {pages}.")
        print(f"Извлечено знаков: {len(document.text)}.")
        print(f"Должность: {profile.title}.")
        print(f"Фактов для подтверждения: {len(profile.facts)}.")
        print(f"Вопросов без ответа: {len(profile.missing_questions)}.")
        for question in profile.missing_questions:
            print(f"- {question.key}: {question.question}")
        return 0

    settings = get_settings()
    try:
        upgrade_database(settings)
        database = create_database(settings)
        try:
            with database.sessions.begin() as session:
                if arguments.command == "import":
                    result = ResumeImportService(session, settings.data_dir).import_file(
                        arguments.account_id,
                        arguments.file,
                        hh_resume_id=arguments.hh_resume_id,
                    )
                elif arguments.command == "facts":
                    pending_facts = ProfileFactService(session).list_pending(arguments.account_id)
                elif arguments.command == "confirm-fact":
                    ProfileFactService(session).confirm(arguments.account_id, arguments.fact_id)
                elif arguments.command == "reject-fact":
                    ProfileFactService(session).reject(arguments.account_id, arguments.fact_id)
                elif arguments.command == "questions":
                    pending = ProfileQuestionService(session).list_pending(arguments.account_id)
                else:
                    answer = input("Ответ: ")
                    ProfileQuestionService(session).answer(
                        arguments.account_id,
                        arguments.key,
                        answer,
                    )
        finally:
            database.close()
    except (FileNotFoundError, LookupError, ResumeDocumentError, RuntimeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    if arguments.command == "import":
        print(f"Импортировано резюме № {result.resume_id}: {result.title}.")
        print(f"Контрольная сумма: {result.source_sha256}.")
        print(f"Фактов ждут подтверждения: {result.facts_pending}.")
        print(f"Вопросов требуют ответа: {len(result.questions_pending)}.")
        print("Файл уже был импортирован." if result.unchanged else "Исходный файл сохранён.")
    elif arguments.command == "questions":
        if pending:
            for question in pending:
                print(f"{question.key}: {question.question}")
        else:
            print("Вопросов без ответа нет.")
    else:
        if arguments.command == "facts":
            if pending_facts:
                for fact in pending_facts:
                    preview = " ".join(fact.content.split())
                    if len(preview) > 160:
                        preview = preview[:157] + "..."
                    print(f"{fact.id} [{fact.category}]: {preview}")
            else:
                print("Фактов, ожидающих подтверждения, нет.")
        elif arguments.command == "confirm-fact":
            print("Факт подтверждён и разрешён к использованию.")
        elif arguments.command == "reject-fact":
            print("Факт отклонён и не будет использоваться.")
        else:
            print("Ответ сохранён и разрешён для автоматического заполнения анкет.")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
