from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from hugin.adapters.resume_documents import ResumeDocumentError, ResumeDocumentReader
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.database.models import CandidateProfileModel, ResumeModel
from hugin.domain.hh import HhResumeDetails
from hugin.services.resume_profile import (
    ProfileFactService,
    ProfileQuestionService,
    ResumeImportService,
    ResumeProfileExtractor,
)

try:
    from playwright.sync_api import Error as PlaywrightError

    from hugin.adapters.hh_browser import VisibleHhBrowser
except ModuleNotFoundError as error:
    if error.name is None or not error.name.startswith("playwright"):
        raise
    PlaywrightError = RuntimeError  # type: ignore[misc,assignment]
    VisibleHhBrowser: Any = None  # type: ignore[no-redef]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Импорт и проверка резюме Hugin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="проверить чтение резюме без записи в базу")
    inspect.add_argument("file", type=Path, help="путь к PDF или DOCX")

    live = subparsers.add_parser(
        "live",
        help="прочитать актуальные поля резюме с hh.ru без сохранения",
    )
    live.add_argument("--account-id", type=positive_int, default=1)
    live.add_argument(
        "--resume-id",
        help="идентификатор резюме hh.ru; без него используется активное резюме из базы",
    )
    live.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="вывести результат в JSON",
    )

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
    if arguments.command == "live":
        try:
            resume_id = arguments.resume_id or _active_hh_resume_id(
                settings,
                arguments.account_id,
            )
            details = _read_live_resume(settings, arguments.account_id, resume_id)
        except (LookupError, PlaywrightError, RuntimeError, SQLAlchemyError, ValueError) as error:
            print(f"Ошибка: {error}", file=sys.stderr)
            return 2
        if arguments.as_json:
            print(json.dumps(asdict(details), ensure_ascii=False, indent=2))
        else:
            _print_live_resume(details)
        return 0

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
                    ProfileFactService(session).confirm(
                        arguments.account_id,
                        arguments.fact_id,
                        allow_in_letters=True,
                        allow_in_forms=True,
                        allow_in_messages=True,
                    )
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


def _active_hh_resume_id(settings: Settings, account_id: int) -> str:
    database = create_database(settings)
    try:
        with database.sessions() as session:
            resume_id = session.scalar(
                select(ResumeModel.hh_id)
                .join(
                    CandidateProfileModel,
                    CandidateProfileModel.active_resume_id == ResumeModel.id,
                )
                .where(
                    CandidateProfileModel.account_id == account_id,
                    ResumeModel.account_id == account_id,
                    ResumeModel.is_active.is_(True),
                )
            )
    finally:
        database.close()
    if not resume_id or resume_id == ResumeImportService.LOCAL_RESUME_ID:
        raise LookupError(
            "Активное резюме hh.ru не выбрано; укажите --resume-id после синхронизации"
        )
    return resume_id


def _read_live_resume(settings: Settings, account_id: int, resume_id: str) -> HhResumeDetails:
    if VisibleHhBrowser is None:
        raise RuntimeError(
            "Команда live требует браузерные компоненты; "
            "установите вариант Hugin с поддержкой браузера"
        )
    with VisibleHhBrowser(
        settings.browser_profile_dir(account_id),
        settings.hh_login_url,
        settings.hh_resumes_url,
        settings.hh_search_url,
        settings.hh_browser_timeout_ms,
    ) as browser:
        browser.open_login()
        if not browser.is_authenticated():
            raise RuntimeError(
                "Вход в hh.ru не выполнен; сначала запустите hugin-hh login --account-id "
                f"{account_id}"
            )
        profile = browser.read_profile()
        if resume_id not in {resume.hh_id for resume in profile.resumes}:
            raise LookupError("Указанное резюме отсутствует в текущем аккаунте hh.ru")
        return browser.read_resume_details(resume_id)


def _print_live_resume(details: HhResumeDetails) -> None:
    fields = (
        ("Идентификатор", details.hh_id),
        ("Название", details.title),
        ("Город", details.city),
        ("Зарплата", details.salary),
        ("Занятость", details.employment),
        ("Формат работы", details.work_format),
        ("Переезд", details.relocation),
        ("Командировки", details.business_trips),
    )
    for label, value in fields:
        print(f"{label}: {value or 'не указано'}")

    print("\nОпыт работы:")
    if details.experience_blocks:
        for index, block in enumerate(details.experience_blocks, start=1):
            heading = " — ".join(value for value in (block.company, block.position) if value)
            print(f"\n{index}. {heading or 'Блок опыта'}")
            if block.period:
                print(block.period)
            content = block.description or block.text
            if content:
                print(content)
    else:
        print(details.experience or "не указано")

    print("\nНавыки:")
    print(details.skills or "не указано")
    print("\nОбразование:")
    print(details.education or "не указано")
    print("\nОбо мне:")
    print(details.about or "не указано")


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
