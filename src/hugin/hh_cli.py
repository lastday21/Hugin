from __future__ import annotations

import argparse
import getpass
import random
import time
from collections.abc import Sequence

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.domain.hh import HhApplyResult, HhApplyStatus, HhProfileData
from hugin.domain.time import local_day_start_utc
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.cover_letter import CoverLetterBuilder
from hugin.services.hh_login import HhCredentials, HhLoginService, LoginStatus
from hugin.services.hh_profile import HhProfileSyncService
from hugin.services.job_search import JobSearchSyncService
from hugin.services.vacancy_analysis import RuleCategory, VacancyAnalysisService

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
        ("sync", "загрузить аккаунт и резюме в базу"),
        ("delete", "удалить сохранённые данные"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--account-id", type=positive_int, default=1)

    search = subparsers.add_parser("search", help="загрузить вакансии по направлению")
    search.add_argument("--account-id", type=positive_int, default=1)
    search.add_argument("--direction", required=True, help="название направления")
    search.add_argument("--resume", required=True, help="точное название резюме")
    search.add_argument("--query", required=True, help="поисковая фраза")
    search.add_argument("--area", default="113", help="идентификатор региона hh.ru")
    search.add_argument("--page", type=non_negative_int, default=0, help="номер страницы")

    analyze = subparsers.add_parser(
        "analyze",
        help="загрузить подробности и проверить найденные вакансии",
    )
    analyze.add_argument("--account-id", type=positive_int, default=1)
    analyze.add_argument("--direction", required=True, help="название направления")
    analyze.add_argument("--limit", type=positive_int, default=20, help="число вакансий")

    apply = subparsers.add_parser("apply", help="автоматически отправить подходящие отклики")
    apply.add_argument("--account-id", type=positive_int, default=1)
    apply.add_argument("--direction", required=True, help="название направления")
    apply.add_argument("--limit", type=positive_int, default=5, help="не более откликов за запуск")
    apply.add_argument(
        "--exclude-stretch",
        action="store_true",
        help="не отправлять пограничные вакансии",
    )
    return parser


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("значение не должно быть отрицательным")
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
        settings.hh_resumes_url,
        settings.hh_search_url,
        settings.hh_browser_timeout_ms,
    )
    with browser:
        login_result = HhLoginService(store).authenticate(arguments.account_id, browser)
        print(STATUS_MESSAGES[login_result.status])
        authenticated = login_result.authenticated
        if login_result.status in {
            LoginStatus.CONFIRMATION_REQUIRED,
            LoginStatus.CAPTCHA_REQUIRED,
            LoginStatus.MANUAL_ACTION_REQUIRED,
        }:
            input("После завершения входа нажмите Enter: ")
            if browser.is_authenticated():
                print(STATUS_MESSAGES[LoginStatus.AUTHENTICATED])
                authenticated = True
        if not authenticated:
            return 2
        if arguments.command == "login":
            return 0
        profile = browser.read_profile()
        if arguments.command == "search":
            search_filters: dict[str, object] = {"order_by": "publication_time"}
            search_result = browser.search_vacancies(
                arguments.query,
                area=arguments.area,
                filters=search_filters,
                page_number=arguments.page,
            )
        if arguments.command == "analyze":
            upgrade_database(settings)
            database = create_database(settings)
            try:
                with database.sessions.begin() as session:
                    HhProfileSyncService(session).synchronize(profile)
                    pending = VacancyAnalysisService(session).pending(
                        account_external_id=profile.external_id,
                        direction_name=arguments.direction,
                        limit=arguments.limit,
                    )
            finally:
                database.close()

            vacancy_details = []
            failures: list[tuple[str, str]] = []
            for vacancy in pending:
                try:
                    vacancy_details.append(browser.read_vacancy_details(vacancy.source_url))
                except RuntimeError as error:
                    failures.append((vacancy.title, str(error)))
        if arguments.command == "apply":
            return _run_applications(arguments, settings, browser, profile)

    upgrade_database(settings)
    if arguments.command == "search":
        database = create_database(settings)
        try:
            with database.sessions.begin() as session:
                HhProfileSyncService(session).synchronize(profile)
                synchronized_search = JobSearchSyncService(session).synchronize(
                    account_external_id=profile.external_id,
                    direction_name=arguments.direction,
                    resume_title=arguments.resume,
                    query=arguments.query,
                    area=arguments.area,
                    filters=search_filters,
                    vacancies=search_result.vacancies,
                )
        finally:
            database.close()

        print(
            f"Направление: {synchronized_search.direction.name} "
            f"(№ {synchronized_search.direction.id})."
        )
        print(f"По запросу найдено на hh.ru: {search_result.found}.")
        print(f"Загружено из текущей страницы: {len(synchronized_search.vacancies)}.")
        for vacancy in synchronized_search.vacancies[:10]:
            employer = f" — {vacancy.employer_name}" if vacancy.employer_name else ""
            print(f"- {vacancy.title}{employer}")
        return 0

    if arguments.command == "analyze":
        database = create_database(settings)
        try:
            with database.sessions.begin() as session:
                analysis_service = VacancyAnalysisService(session)
                analysis_service.synchronize(
                    account_external_id=profile.external_id,
                    direction_name=arguments.direction,
                    vacancies=tuple(vacancy_details),
                )
                analyzed = analysis_service.reanalyze(
                    account_external_id=profile.external_id,
                    direction_name=arguments.direction,
                )
        finally:
            database.close()

        matched = sum(result.evaluation.category is RuleCategory.MATCH for result in analyzed)
        stretch = sum(result.evaluation.category is RuleCategory.STRETCH for result in analyzed)
        rejected = len(analyzed) - matched - stretch
        print(f"Проверено вакансий: {len(analyzed)}.")
        print(f"Подходят: {matched}. Пограничные: {stretch}. Отклонены: {rejected}.")
        for analysis_result in analyzed:
            evaluation = analysis_result.evaluation
            decisions = {
                RuleCategory.MATCH: "подходит",
                RuleCategory.STRETCH: "условно подходит",
                RuleCategory.REJECTED: "отклонена",
            }
            decision = decisions[evaluation.category]
            reasons = "; ".join(evaluation.reasons)
            print(
                f"- {analysis_result.vacancy.title}: {decision}, {evaluation.score:.0f}. {reasons}"
            )
        for title, failure_message in failures:
            print(f"- Пропущена вакансия «{title}»: {failure_message}")
        return 0

    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            synchronized = HhProfileSyncService(session).synchronize(profile)
    finally:
        database.close()

    print(f"Аккаунт в базе: {synchronized.account.label} (№ {synchronized.account.id}).")
    print(f"Загружено резюме: {len(synchronized.resumes)}.")
    for resume in synchronized.resumes:
        print(f"- {resume.title} ({resume.hh_id})")
    return 0


def _run_applications(
    arguments: argparse.Namespace,
    settings: Settings,
    browser: VisibleHhBrowser,
    profile: HhProfileData,
) -> int:
    upgrade_database(settings)
    database = create_database(settings)
    blocking = False
    sent = 0
    try:
        with database.sessions.begin() as session:
            HhProfileSyncService(session).synchronize(profile)
            service = ApplicationAutomationService(session)
            service.resume_after_authentication()
            prepared = service.prepare(
                account_external_id=profile.external_id,
                direction_name=arguments.direction,
                include_stretch=not arguments.exclude_stretch,
            )
            day_start = local_day_start_utc()
            sent_today = service.applied_since(prepared.account_id, day_start)

        resume_details = browser.read_resume_details(prepared.resume.hh_id)
        available_today = max(settings.hh_apply_daily_limit - sent_today, 0)
        run_limit = min(arguments.limit, available_today)
        print(
            f"Подготовлено новых заданий: {prepared.created}. "
            f"Уже существовало: {prepared.existing}."
        )
        print(f"Сегодня уже отправлено: {sent_today}. Ограничение на этот запуск: {run_limit}.")
        if run_limit == 0:
            print("Дневное ограничение исчерпано, новые отклики не отправлены.")
            return 0

        letter_builder = CoverLetterBuilder()
        while sent < run_limit:
            with database.sessions.begin() as session:
                job = ApplicationAutomationService(session).claim_next(prepared.direction_id)
            if job is None:
                break
            raw_category = job.direction_vacancy.rules_details.get("category")
            category = RuleCategory(str(raw_category))
            letter = letter_builder.build(job.vacancy, resume_details, category)
            try:
                result = browser.apply_to_vacancy(
                    job.vacancy.source_url,
                    expected_resume_title=job.resume.title,
                    cover_letter=letter,
                )
            except Exception as error:
                result = HhApplyResult(
                    HhApplyStatus.UNKNOWN_RESULT,
                    job.vacancy.source_url,
                    f"Ошибка выполнения: {type(error).__name__}",
                )
            with database.sessions.begin() as session:
                recorded = ApplicationAutomationService(session).record_result(job, result)
            status_text = _apply_status_text(result.status)
            print(f"- {job.vacancy.title}: {status_text}.")
            if result.questions:
                print(f"  Вопросов работодателя без надёжных ответов: {len(result.questions)}.")
            if recorded.sent:
                sent += 1
            if recorded.blocking:
                blocking = True
                break
            if recorded.sent and sent < run_limit:
                delay = random.uniform(
                    settings.hh_apply_delay_min_seconds,
                    settings.hh_apply_delay_max_seconds,
                )
                time.sleep(delay)
    finally:
        database.close()

    print(f"Новых подтверждённых откликов: {sent}.")
    if blocking:
        print("Работа остановлена: требуется проверить состояние hh.ru.")
        return 3
    return 0


def _apply_status_text(status: HhApplyStatus) -> str:
    messages = {
        HhApplyStatus.APPLIED: "отклик подтверждён",
        HhApplyStatus.ALREADY_APPLIED: "отклик уже был отправлен",
        HhApplyStatus.QUESTIONS_REQUIRED: "требуется заполнить анкету",
        HhApplyStatus.VACANCY_CLOSED: "вакансия закрыта",
        HhApplyStatus.AUTH_REQUIRED: "требуется повторный вход",
        HhApplyStatus.CAPTCHA_REQUIRED: "требуется проверка",
        HhApplyStatus.ACCOUNT_WARNING: "получено предупреждение аккаунта",
        HhApplyStatus.RESUME_MISMATCH: "выбрано неверное резюме",
        HhApplyStatus.RETRYABLE_ERROR: "страница временно недоступна",
        HhApplyStatus.UNKNOWN_RESULT: "результат отправки не подтверждён",
    }
    return messages[status]


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
