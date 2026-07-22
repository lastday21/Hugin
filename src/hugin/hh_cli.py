from __future__ import annotations

import argparse
import getpass
import random
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from hugin.adapters.credentials import WindowsCredentialStore
from hugin.adapters.hh_browser import VisibleHhBrowser
from hugin.core.settings import Settings, get_settings
from hugin.database import create_database, upgrade_database
from hugin.domain.directions import EmploymentForm, SearchRegion, WorkFormat
from hugin.domain.hh import HhApplyResult, HhApplyStatus, HhProfileData
from hugin.domain.resumes import ProfileQuestionCandidate
from hugin.domain.time import local_day_start_utc, local_timezone_name
from hugin.services.application_automation import ApplicationAutomationService
from hugin.services.career_directions import (
    COMMON_REGIONS,
    CareerDirectionService,
    DirectionSearchSettings,
    VacancySearchTask,
)
from hugin.services.hh_login import HhCredentials, HhLoginService, LoginStatus
from hugin.services.hh_profile import HhProfileSyncService
from hugin.services.job_search import JobSearchSyncService
from hugin.services.queue import QueueService
from hugin.services.resume_profile import ProfileQuestionService
from hugin.services.vacancy_analysis import RuleCategory, VacancyAnalysisService
from hugin.services.vacancy_review import VacancyReviewEntry, VacancyReviewService

STATUS_MESSAGES = {
    LoginStatus.AUTHENTICATED: "Вход в hh.ru выполнен.",
    LoginStatus.CREDENTIALS_REQUIRED: "Сначала сохраните данные командой hugin-hh save.",
    LoginStatus.CONFIRMATION_REQUIRED: "Введите одноразовый код в открытом окне.",
    LoginStatus.CAPTCHA_REQUIRED: "Пройдите проверку в открытом окне.",
    LoginStatus.INVALID_CREDENTIALS: "hh.ru отклонил логин или пароль.",
    LoginStatus.MANUAL_ACTION_REQUIRED: "Завершите вход в открытом окне.",
}

DISPLAY_TRANSLATION: dict[int, str] = {
    ord("\u00a0"): " ",
    ord("\u2010"): "-",
    ord("\u2011"): "-",
    ord("\u2012"): "-",
    ord("\u2013"): "-",
    ord("\u2014"): "-",
    ord("\u202f"): " ",
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
    search.add_argument(
        "--resume",
        help="точное название резюме; по умолчанию используется активное ИТ-резюме",
    )
    search.add_argument(
        "--query",
        action="append",
        help="разовый поисковый запрос; без него используются настройки направления",
    )
    search.add_argument(
        "--area",
        action="append",
        help="регион для разового поиска; без него используется Россия (113)",
    )
    search.add_argument("--page", type=non_negative_int, default=0, help="номер страницы")
    search.add_argument(
        "--pages",
        type=positive_int,
        default=1,
        help="сколько последовательных страниц обойти",
    )

    configure = subparsers.add_parser(
        "configure-search",
        help="настроить направление, запросы, города и условия поиска",
    )
    configure.add_argument("--account-id", type=positive_int, default=1)
    configure.add_argument("--direction", required=True, help="название направления")
    configure.add_argument(
        "--query",
        action="append",
        help="поисковая фраза; можно указать несколько раз",
    )
    configure.add_argument(
        "--city",
        action="append",
        type=search_region,
        default=[],
        help="город из встроенного списка или в виде Название=код_hh",
    )
    configure.add_argument(
        "--format",
        dest="work_formats",
        action="append",
        type=work_format,
        help="удалённо, офис или гибрид; без параметра берётся из резюме",
    )
    configure.add_argument(
        "--employment",
        dest="employment_forms",
        action="append",
        type=employment_form,
        help="полная, частичная, проект или вахта; без параметра берётся из резюме",
    )
    configure.add_argument("--minimum-salary", type=positive_int)
    configure.add_argument("--desired-salary", type=positive_int)
    configure.add_argument(
        "--remote-russia",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="для удалённой работы искать по всей России",
    )
    configure.add_argument(
        "--schedule-minutes",
        type=positive_int,
        default=120,
        help="интервал поиска в минутах",
    )

    show_search = subparsers.add_parser("show-search", help="показать настройки направления")
    show_search.add_argument("--account-id", type=positive_int, default=1)
    show_search.add_argument("--direction", required=True, help="название направления")

    subparsers.add_parser("cities", help="показать города, для которых код hh.ru уже известен")

    analyze = subparsers.add_parser(
        "analyze",
        help="загрузить подробности и проверить найденные вакансии",
    )
    analyze.add_argument("--account-id", type=positive_int, default=1)
    analyze.add_argument("--direction", required=True, help="название направления")
    analyze.add_argument("--limit", type=positive_int, default=20, help="число вакансий")

    rejected = subparsers.add_parser("rejected", help="показать отклонённые вакансии")
    rejected.add_argument("--account-id", type=positive_int, default=1)
    rejected.add_argument("--direction", required=True, help="название направления")
    rejected.add_argument("--limit", type=positive_int, default=50)
    rejected.add_argument("--company", help="часть названия компании")
    rejected.add_argument("--region", help="часть названия города или региона")
    rejected.add_argument("--reason", help="часть причины отклонения")
    rejected.add_argument("--sort", choices=("newest", "score", "company"), default="newest")

    vacancy = subparsers.add_parser("vacancy", help="показать сохранённую карточку вакансии")
    vacancy.add_argument("--account-id", type=positive_int, default=1)
    vacancy.add_argument("--direction", required=True, help="название направления")
    vacancy.add_argument("--vacancy-id", required=True, help="идентификатор вакансии hh.ru")

    restore = subparsers.add_parser("restore", help="вернуть отклонённую вакансию в обработку")
    restore.add_argument("--account-id", type=positive_int, default=1)
    restore.add_argument("--direction", required=True, help="название направления")
    restore.add_argument("--vacancy-id", required=True, help="идентификатор вакансии hh.ru")

    configure_queue = subparsers.add_parser(
        "configure-queue",
        help="сохранить суточное ограничение и интервалы",
    )
    configure_queue.add_argument("--daily-limit", type=positive_int)
    configure_queue.add_argument("--delay-min", type=non_negative_int)
    configure_queue.add_argument("--delay-max", type=non_negative_int)
    subparsers.add_parser("queue-status", help="показать состояние очереди")
    subparsers.add_parser("pause", help="приостановить новые отклики")
    subparsers.add_parser("resume", help="продолжить обработку очереди")

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


def search_region(value: str) -> SearchRegion:
    normalized = " ".join(value.strip().split())
    known = COMMON_REGIONS.get(normalized.casefold())
    if known is not None:
        return known
    if "=" not in normalized:
        raise argparse.ArgumentTypeError(
            "неизвестный город; используйте команду cities или формат Название=код_hh"
        )
    name, area = (part.strip() for part in normalized.rsplit("=", 1))
    if not name or not area.isdigit():
        raise argparse.ArgumentTypeError("город нужно указать в формате Название=код_hh")
    return SearchRegion(area, name)


def work_format(value: str) -> WorkFormat:
    aliases = {
        "удаленно": WorkFormat.REMOTE,
        "удалённо": WorkFormat.REMOTE,
        "офис": WorkFormat.ON_SITE,
        "гибрид": WorkFormat.HYBRID,
    }
    try:
        return aliases[value.strip().casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("формат: удалённо, офис или гибрид") from error


def employment_form(value: str) -> EmploymentForm:
    aliases = {
        "полная": EmploymentForm.FULL,
        "частичная": EmploymentForm.PART,
        "проект": EmploymentForm.PROJECT,
        "вахта": EmploymentForm.FLY_IN_FLY_OUT,
    }
    try:
        return aliases[value.strip().casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError(
            "занятость: полная, частичная, проект или вахта"
        ) from error


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    if arguments.command == "cities":
        unique = {region.area: region for region in COMMON_REGIONS.values()}
        for region in sorted(unique.values(), key=lambda item: item.name):
            print(f"{region.name}: {region.area}")
        return 0

    if arguments.command in {"configure-search", "show-search"}:
        return _run_search_settings(arguments)
    if arguments.command in {"rejected", "vacancy", "restore"}:
        return _run_vacancy_review(arguments)
    if arguments.command in {"configure-queue", "queue-status", "pause", "resume"}:
        return _run_queue_control(arguments)

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
    search_tasks: tuple[VacancySearchTask, ...] = ()
    if arguments.command == "search":
        search_tasks = _search_tasks(arguments, settings)
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
            search_runs = []
            for task in search_tasks:
                for page_number in range(arguments.page, arguments.page + arguments.pages):
                    search_result = browser.search_vacancies(
                        task.query,
                        area=task.area,
                        filters=task.filters,
                        page_number=page_number,
                    )
                    search_runs.append((task, search_result))
                    if not search_result.vacancies:
                        break
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
                synchronized_runs = tuple(
                    (
                        task,
                        search_result,
                        JobSearchSyncService(session).synchronize(
                            account_external_id=profile.external_id,
                            direction_name=arguments.direction,
                            resume_title=arguments.resume,
                            query=task.query,
                            area=task.area,
                            region=task.region_name,
                            search_query_id=task.search_query_id,
                            filters=task.filters,
                            vacancies=search_result.vacancies,
                        ),
                    )
                    for task, search_result in search_runs
                )
        finally:
            database.close()

        first = synchronized_runs[0][2]
        print(f"Направление: {first.direction.name} (№ {first.direction.id}).")
        print(f"Выполнено вариантов поиска: {len(search_tasks)}.")
        print(f"Обработано страниц поиска: {len(synchronized_runs)}.")
        for task, search_result, synced_run in synchronized_runs:
            print(
                f"- {task.query}; {task.region_name}: найдено {search_result.found}, "
                f"загружено {len(synced_run.vacancies)}"
            )
        unique_vacancies = {
            vacancy.id: vacancy
            for _, _, synced_run in synchronized_runs
            for vacancy in synced_run.vacancies
        }
        print(f"Уникальных вакансий в базе: {len(unique_vacancies)}.")
        for vacancy in tuple(unique_vacancies.values())[:10]:
            employer = f" — {_display_text(vacancy.employer_name)}" if vacancy.employer_name else ""
            print(f"- {_display_text(vacancy.title)}{employer}")
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
                queued = ApplicationAutomationService(session).prepare(
                    account_external_id=profile.external_id,
                    direction_name=arguments.direction,
                    include_stretch=True,
                )
        finally:
            database.close()

        matched = sum(result.evaluation.category is RuleCategory.MATCH for result in analyzed)
        stretch = sum(result.evaluation.category is RuleCategory.STRETCH for result in analyzed)
        duplicates = sum(result.vacancy.duplicate_of_id is not None for result in analyzed)
        rejected = len(analyzed) - matched - stretch
        print(f"Проверено вакансий: {len(analyzed)}.")
        print(
            f"Подходят: {matched}. Пограничные: {stretch}. "
            f"Отклонены: {rejected}. Из них похожих публикаций: {duplicates}."
        )
        print(
            f"Добавлено в очередь: {queued.created}. Уже находилось в обработке: {queued.existing}."
        )
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
                f"- {_display_text(analysis_result.vacancy.title)}: "
                f"{decision}, {evaluation.score:.0f}. {reasons}"
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


def _run_search_settings(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = CareerDirectionService(session)
            if arguments.command == "configure-search":
                configured = service.configure(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    queries=tuple(arguments.query) if arguments.query else None,
                    regions=tuple(arguments.city),
                    work_formats=(
                        tuple(arguments.work_formats)
                        if arguments.work_formats is not None
                        else None
                    ),
                    employment_forms=(
                        tuple(arguments.employment_forms)
                        if arguments.employment_forms is not None
                        else None
                    ),
                    minimum_salary=arguments.minimum_salary,
                    desired_salary=arguments.desired_salary,
                    remote_all_russia=arguments.remote_russia,
                    schedule_minutes=arguments.schedule_minutes,
                )
            else:
                configured = service.get(arguments.account_id, arguments.direction)
            pending_questions = ProfileQuestionService(session).list_pending(arguments.account_id)
    finally:
        database.close()

    _print_search_settings(configured)
    _print_pending_questions(pending_questions)
    return 0


def _run_queue_control(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = QueueService(session)
            if arguments.command == "configure-queue":
                service.configure(
                    timezone_name=local_timezone_name(),
                    daily_limit=arguments.daily_limit,
                    delay_min_seconds=arguments.delay_min,
                    delay_max_seconds=arguments.delay_max,
                )
            elif arguments.command == "pause":
                service.pause()
            elif arguments.command == "resume":
                service.resume()
            else:
                service.policy(local_timezone_name())
            status = service.status()
    finally:
        database.close()

    state_names = {
        "RUNNING": "работает",
        "PAUSED": "приостановлена",
        "AUTH_REQUIRED": "требуется вход",
        "CAPTCHA_REQUIRED": "требуется проверка",
        "ACCOUNT_WARNING": "предупреждение аккаунта",
    }
    print(f"Очередь: {state_names[status.system.state.value]}.")
    print(
        f"Суточное ограничение: {status.policy.daily_limit}. "
        f"Интервал: {status.policy.delay_min_seconds}-"
        f"{status.policy.delay_max_seconds} секунд."
    )
    print(f"Часовой пояс: {status.policy.timezone_name}.")
    if status.system.next_apply_at is not None:
        print(f"Следующий отклик не раньше: {status.system.next_apply_at.isoformat()}.")
    if status.task_counts:
        counts = ", ".join(
            f"{state.value}: {count}"
            for state, count in sorted(status.task_counts.items(), key=lambda item: item[0].value)
        )
        print(f"Задания: {counts}.")
    else:
        print("Заданий пока нет.")
    return 0


def _run_vacancy_review(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            service = VacancyReviewService(session)
            if arguments.command == "rejected":
                entries = service.list_rejected(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    limit=arguments.limit,
                    company=arguments.company,
                    region=arguments.region,
                    reason=arguments.reason,
                    sort=arguments.sort,
                )
            elif arguments.command == "restore":
                entry = service.restore(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    hh_id=arguments.vacancy_id,
                )
                prepared = ApplicationAutomationService(session).prepare_for_account_id(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    include_stretch=True,
                )
                entries = (entry,)
            else:
                entry = service.get_card(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    hh_id=arguments.vacancy_id,
                )
                entries = (entry,)
    finally:
        database.close()

    if arguments.command == "rejected":
        print(f"Отклонённых вакансий: {len(entries)}.")
        for entry in entries:
            reasons = "; ".join(_rule_reasons(entry))
            employer = entry.vacancy.employer_name or "компания не указана"
            region = entry.vacancy.region or "регион не указан"
            score = entry.tracking.rules_score or 0
            print(
                f"- {entry.vacancy.hh_id}: {_display_text(entry.vacancy.title)} — "
                f"{_display_text(employer)}; {_display_text(region)}; "
                f"оценка {score:.0f}; {reasons}"
            )
        return 0

    if arguments.command == "restore":
        print(
            f"Вакансия {entries[0].vacancy.hh_id} возвращена в очередь. "
            f"Новых заданий: {prepared.created}. Решение пользователя сохранено."
        )
        return 0

    _print_vacancy_card(entries[0])
    return 0


def _rule_reasons(entry: VacancyReviewEntry) -> tuple[str, ...]:
    raw = entry.tracking.rules_details.get("reasons", [])
    return tuple(str(value) for value in raw) if isinstance(raw, list) else ()


def _print_vacancy_card(entry: VacancyReviewEntry) -> None:
    vacancy = entry.vacancy
    title = _display_text(vacancy.title)
    employer = _display_text(vacancy.employer_name or "компания не указана")
    print(f"{title} — {employer}")
    print(f"hh.ru: {vacancy.source_url}")
    print(f"Состояние: {entry.tracking.state.value}; доступность: {vacancy.availability.value}")
    print(
        f"Регион: {_display_text(vacancy.region or 'не указан')}; "
        f"адрес: {_display_text(vacancy.address or 'не указан')}"
    )
    salary_from = str(vacancy.salary_from) if vacancy.salary_from is not None else "—"
    salary_to = str(vacancy.salary_to) if vacancy.salary_to is not None else "—"
    print(f"Зарплата: {salary_from}–{salary_to} {vacancy.salary_currency or ''}".rstrip())
    print(
        "Условия: "
        f"{_display_text(vacancy.work_format or 'формат не указан')}; "
        f"{_display_text(vacancy.employment or 'занятость не указана')}; "
        f"{_display_text(vacancy.schedule or 'график не указан')}"
    )
    print(f"Опыт: {_display_text(vacancy.experience or 'не указан')}")
    print("Навыки: " + _display_text(", ".join(vacancy.key_skills) or "не указаны"))
    print("Причины решения: " + ("; ".join(_rule_reasons(entry)) or "ещё не проверена"))
    if vacancy.duplicate_of_id is not None:
        print(f"Повтор основной вакансии в базе: № {vacancy.duplicate_of_id}")
    print("Найдена по:")
    for discovery in entry.discoveries:
        print(
            f"- {discovery.query_text}; {discovery.region}; {discovery.discovered_at.isoformat()}"
        )
    print(f"Изменений сохранено: {len(entry.changes)}.")
    if vacancy.description:
        print("Описание:")
        print(_display_text(vacancy.description))


def _display_text(value: object) -> str:
    return str(value).translate(DISPLAY_TRANSLATION)


def _search_tasks(
    arguments: argparse.Namespace,
    settings: Settings,
) -> tuple[VacancySearchTask, ...]:
    if arguments.query:
        areas = arguments.area or ["113"]
        return tuple(
            VacancySearchTask(
                search_query_id=None,
                query=query,
                area=area,
                region_name="Россия" if area == "113" else f"регион {area}",
                filters={"order_by": "publication_time"},
            )
            for query in arguments.query
            for area in areas
        )
    if arguments.area:
        raise ValueError("Параметр --area можно использовать только вместе с --query")

    upgrade_database(settings)
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            tasks = CareerDirectionService(session).build_search_tasks(
                arguments.account_id,
                arguments.direction,
            )
    finally:
        database.close()
    if not tasks:
        raise LookupError("В настройках направления не получилось собрать ни одного поиска")
    return tasks


def _print_search_settings(settings: DirectionSearchSettings) -> None:
    format_names = {
        WorkFormat.REMOTE: "удалённо",
        WorkFormat.ON_SITE: "офис",
        WorkFormat.HYBRID: "гибрид",
    }
    employment_names = {
        EmploymentForm.FULL: "полная",
        EmploymentForm.PART: "частичная",
        EmploymentForm.PROJECT: "проект",
        EmploymentForm.FLY_IN_FLY_OUT: "вахта",
    }
    regions = {region.area: region.name for query in settings.queries for region in query.regions}
    print(f"Направление: {settings.direction.name} (№ {settings.direction.id})")
    print(f"Активное резюме: {settings.resume.title}")
    print("Запросы: " + "; ".join(query.query for query in settings.queries))
    print("Города: " + (", ".join(regions.values()) or "не указаны"))
    print(
        "Форматы: "
        + (", ".join(format_names[value] for value in settings.work_formats) or "без ограничения")
    )
    print(
        "Занятость: "
        + (
            ", ".join(employment_names[value] for value in settings.employment_forms)
            or "без ограничения"
        )
    )
    minimum = settings.minimum_salary or "не указана"
    desired = settings.desired_salary or "не указана"
    print(f"Зарплата: минимум {minimum}, желаемая {desired} {settings.salary_currency}")
    print(
        "Удалённый поиск по всей России: "
        + ("включён" if settings.remote_all_russia else "выключен")
    )
    print(
        "Навыки: берутся из подтверждённых данных активного резюме "
        f"({len(settings.skills_from_resume)} блоков)"
    )


def _print_pending_questions(questions: tuple[ProfileQuestionCandidate, ...]) -> None:
    if not questions:
        return
    print("В резюме не найдены некоторые ответы:")
    for question in questions:
        print(f"- {question.key}: {question.question}")
    print(
        "Их можно сохранить командой hugin-resume answer --key КЛЮЧ. "
        "Без ответа поиск продолжится без соответствующего ограничения."
    )


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
            recovered = service.recover_interrupted()
            policy = service.policy(local_timezone_name())
            prepared = service.prepare(
                account_external_id=profile.external_id,
                direction_name=arguments.direction,
                include_stretch=not arguments.exclude_stretch,
            )
            day_start = local_day_start_utc()
            sent_today = service.applied_since(prepared.account_id, day_start)

        available_today = max(policy.daily_limit - sent_today, 0)
        run_limit = min(arguments.limit, available_today)
        print(
            f"Подготовлено новых заданий: {prepared.created}. "
            f"Уже существовало: {prepared.existing}."
        )
        if recovered:
            print(
                f"После прошлого прерывания требуют сверки: {recovered}. "
                "Остальная очередь продолжит работу."
            )
        print(f"Сегодня уже отправлено: {sent_today}. Ограничение на этот запуск: {run_limit}.")
        if run_limit == 0:
            print("Дневное ограничение исчерпано, новые отклики не отправлены.")
            return 0

        while sent < run_limit:
            with database.sessions.begin() as session:
                job = ApplicationAutomationService(session).claim_next(
                    prepared.direction_id,
                    require_cover_letter=True,
                )
            if job is None:
                break
            if not job.cover_letter:
                raise RuntimeError("Готовое сопроводительное письмо отсутствует")
            try:
                result = browser.apply_to_vacancy(
                    job.vacancy.source_url,
                    expected_resume_title=job.resume.title,
                    cover_letter=job.cover_letter,
                )
            except Exception as error:
                result = HhApplyResult(
                    HhApplyStatus.UNKNOWN_RESULT,
                    job.vacancy.source_url,
                    f"Ошибка выполнения: {type(error).__name__}",
                )
            apply_delay = None
            if result.status is HhApplyStatus.APPLIED:
                apply_delay = timedelta(
                    seconds=random.uniform(
                        policy.delay_min_seconds,
                        policy.delay_max_seconds,
                    )
                )
            with database.sessions.begin() as session:
                recorded = ApplicationAutomationService(session).record_result(
                    job,
                    result,
                    apply_delay=apply_delay,
                )
            status_text = _apply_status_text(result.status)
            print(f"- {job.vacancy.title}: {status_text}.")
            if result.questions:
                print(f"  Вопросов работодателя без надёжных ответов: {len(result.questions)}.")
            if recorded.sent:
                sent += 1
            if recorded.blocking:
                blocking = True
                break
            if recorded.sent and sent < run_limit and recorded.next_apply_at is not None:
                wait_seconds = max(
                    (recorded.next_apply_at - datetime.now(UTC)).total_seconds(),
                    0,
                )
                print(f"  Пауза до следующего отклика: {wait_seconds:.0f} секунд.")
                time.sleep(wait_seconds)
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
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
