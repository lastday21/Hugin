from __future__ import annotations

import argparse
import getpass
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from hugin.adapters.codex_cli import CodexCliError, configured_codex_cli_client
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
    CoverLetterRejectionModel,
    VacancyModel,
)
from hugin.diagnostics import OperationJournal
from hugin.domain.content import cover_letter_instruction_version
from hugin.services.ai_prompts import AiPromptSettingsService
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

    reject = subparsers.add_parser(
        "reject",
        help="отклонить проверенное письмо и разрешить создать новое",
    )
    reject.add_argument("--account-id", type=positive_int, default=1)
    reject.add_argument("--letter-id", type=positive_int, required=True)
    reject.add_argument("--reason", required=True, help="краткая причина отклонения")
    reject.add_argument(
        "--fragment",
        help="конкретное спорное предложение или фраза",
    )

    replace = subparsers.add_parser(
        "replace",
        help="проверить и сохранить исправленный вручную текст письма",
    )
    replace.add_argument("--account-id", type=positive_int, default=1)
    replace.add_argument("--letter-id", type=positive_int, required=True)
    replace.add_argument("--file", type=Path, required=True, help="текстовый файл UTF-8")

    quality_trial = subparsers.add_parser(
        "quality-trial",
        help="проверить новый порядок создания писем без сохранения и отправки",
    )
    quality_trial.add_argument("--account-id", type=positive_int, default=1)
    quality_trial.add_argument("--direction", required=True, help="точное название направления")
    quality_trial.add_argument("--limit", type=positive_int, default=10)
    quality_trial.add_argument("--vacancy-id", help="проверить только этот номер вакансии hh.ru")
    quality_trial.add_argument(
        "--exclude-stretch",
        action="store_true",
        help="не включать смежные вакансии",
    )
    quality_trial.add_argument(
        "--completed",
        action="store_true",
        help="взять ранее завершённые отклики вместо текущей очереди",
    )

    sent_quality = subparsers.add_parser(
        "sent-quality-trial",
        help="оценить качество уже отправленных писем без изменения данных",
    )
    sent_quality.add_argument("--account-id", type=positive_int, default=1)
    sent_quality.add_argument("--limit", type=positive_int, default=25)
    sent_quality.add_argument(
        "--show-text",
        action="store_true",
        help="показать полный текст каждого письма",
    )
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
            client = _client(settings, store, operation="connection_check")
            response = client.complete(SYSTEM_PROMPT, "Ответь одним словом: готово")
            print(f"YandexGPT доступен: {client.model_name}: {response}")
            return 0

        upgrade_database(settings)
        database = create_database(settings)
        try:
            if arguments.command == "sent-quality-trial":
                quality_model = configured_codex_cli_client(
                    settings,
                    operation="cover_letter_quality_trial_check",
                )
                with database.sessions.begin() as session:
                    sent_result = CoverLetterService(
                        session,
                        quality_model=quality_model,
                    ).assess_sent_quality(
                        account_id=arguments.account_id,
                        limit=arguments.limit,
                    )
                for index, sent_item in enumerate(sent_result.items, start=1):
                    score = f"{sent_item.score}/10" if sent_item.score is not None else "ошибка"
                    verdict = "прошло" if sent_item.passed else "ниже порога"
                    print(
                        f"{index}. Письмо № {sent_item.letter_id}, вакансия № {sent_item.hh_id}, "
                        f"{sent_item.title} [{sent_item.category}]: {score}, {verdict}."
                    )
                    if sent_item.score is not None:
                        print(
                            f"Структура {sent_item.structure}/3, ясность {sent_item.clarity}/3, "
                            f"самостоятельность {sent_item.individuality}/2, "
                            f"естественность {sent_item.naturalness}/2."
                        )
                    print(f"Причина: {sent_item.reason}")
                    if arguments.show_text:
                        print(sent_item.text)
                    print("-----")
                print(
                    f"Проверено: {len(sent_result.items)}. Не ниже 9: {sent_result.passed}. "
                    f"Ошибок: {sent_result.failed}."
                )
                print("Данные не изменены, на hh.ru ничего не отправлено.")
                return 3 if sent_result.failed else 0
            if arguments.command == "quality-trial":
                writer = configured_codex_cli_client(
                    settings,
                    operation="cover_letter_quality_trial_writer",
                )
                quality_model = configured_codex_cli_client(
                    settings,
                    operation="cover_letter_quality_trial_check",
                )
                with database.sessions.begin() as session:
                    trial_result = CoverLetterService(
                        session,
                        writer,
                        quality_model=quality_model,
                    ).trial_quality(
                        account_id=arguments.account_id,
                        direction_name=arguments.direction,
                        limit=arguments.limit,
                        include_stretch=not arguments.exclude_stretch,
                        completed=arguments.completed,
                        vacancy_hh_id=arguments.vacancy_id,
                    )
                action_labels = {
                    "passed": "прошло сразу",
                    "corrected": "исправлено и прошло",
                    "blocked": "остановлено",
                    "failed": "техническая ошибка",
                }
                for index, trial_item in enumerate(trial_result.items, start=1):
                    scores = ""
                    if trial_item.initial_score is not None:
                        scores = f", первая оценка {trial_item.initial_score}/10"
                    if (
                        trial_item.final_score is not None
                        and trial_item.final_score != trial_item.initial_score
                    ):
                        scores += f", итоговая {trial_item.final_score}/10"
                    print(
                        f"{index}. № {trial_item.hh_id}, {trial_item.title} "
                        f"[{trial_item.category}]: {action_labels[trial_item.action]}{scores}."
                    )
                    if trial_item.reason:
                        print(f"Причина: {trial_item.reason}")
                    if trial_item.text:
                        print(trial_item.text)
                    print("-----")
                print(
                    f"Проверено: {len(trial_result.items)}. Прошло: {trial_result.passed}. "
                    f"Остановлено: {trial_result.blocked}. Ошибок: {trial_result.failed}."
                )
                print(
                    "Письма не сохранены, состояние очереди не изменено, "
                    "на hh.ru ничего не отправлено."
                )
                return 3 if trial_result.failed else 0
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
                    instruction_version = cover_letter_instruction_version(
                        AiPromptSettingsService(session).get().cover_letter
                    )
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
                            CoverLetterModel.instruction_version == instruction_version,
                        )
                        .order_by(CoverLetterModel.id.desc())
                        .limit(1)
                    ).first()
                    if row is None:
                        raise LookupError("Письмо для этой вакансии не найдено")
                    letter, vacancy = row
                    rejections = tuple(
                        session.scalars(
                            select(CoverLetterRejectionModel)
                            .where(CoverLetterRejectionModel.cover_letter_id == letter.id)
                            .order_by(CoverLetterRejectionModel.sequence_number)
                        )
                    )
                    print(f"Вакансия: {vacancy.title} (№ {vacancy.hh_id})")
                    print(f"Идентификатор письма: {letter.id}")
                    print(f"Состояние: {letter.state.value}")
                    print(f"Способ подготовки: {letter.generation_mode.value}")
                    print(f"Модель текста: {letter.model_name}")
                    if letter.reused_from_id is not None:
                        print(f"Исходное письмо: № {letter.reused_from_id}")
                    if letter.router_model_name:
                        print(f"Модель отбора: {letter.router_model_name}")
                    if letter.router_confidence is not None:
                        print(f"Уверенность отбора: {letter.router_confidence:.2f}")
                    if letter.router_reason:
                        print(f"Причина решения: {letter.router_reason}")
                    if letter.text:
                        digest = hashlib.sha256(letter.text.encode("utf-8")).hexdigest()
                        print(f"SHA256 письма: {digest}")
                        print(letter.text)
                    elif letter.failure_reason:
                        print(f"Причина: {letter.failure_reason}")
                    for rejection in rejections:
                        print("-----")
                        print(f"Отклонённый вариант № {rejection.sequence_number}")
                        print(f"Код причины: {rejection.reason_code}")
                        print(f"Причина: {rejection.reason_message}")
                        if rejection.rejected_fragment:
                            print(f"Спорная фраза: {rejection.rejected_fragment}")
                        print(rejection.text)
                return 0
            if arguments.command == "reject":
                reason = " ".join(arguments.reason.split())
                if not reason:
                    raise ValueError("Причина отклонения не может быть пустой")
                fragment = (
                    " ".join(arguments.fragment.split()) if arguments.fragment is not None else None
                )
                with database.sessions.begin() as session:
                    CoverLetterService(session).reject_reviewed(
                        account_id=arguments.account_id,
                        letter_id=arguments.letter_id,
                        reason=reason,
                        rejected_fragment=fragment,
                    )
                print(
                    f"Письмо № {arguments.letter_id} отклонено. "
                    "Для вакансии можно создать новый вариант."
                )
                return 0
            if arguments.command == "replace":
                path = arguments.file.resolve()
                if not path.is_file():
                    raise ValueError("Файл с письмом не найден")
                text = path.read_text(encoding="utf-8")
                with database.sessions.begin() as session:
                    letter = CoverLetterService(session).save_reviewed(
                        account_id=arguments.account_id,
                        letter_id=arguments.letter_id,
                        text=text,
                    )
                digest = hashlib.sha256((letter.text or "").encode("utf-8")).hexdigest()
                OperationJournal(settings.data_dir).record(
                    "applications",
                    "cover_letter.manual_review",
                    status="completed",
                    account_id=arguments.account_id,
                    letter_id=letter.id,
                    letter_sha256=digest,
                )
                print(
                    f"Исправленное письмо № {letter.id} прошло проверки и сохранено. "
                    f"SHA256: {digest}"
                )
                return 0

            with database.sessions.begin() as session:
                ai_settings = AiPromptSettingsService(session)
                client = _client(
                    settings,
                    store,
                    model=ai_settings.get_model(),
                    reasoning_effort=ai_settings.get_reasoning_effort(),
                    operation="cover_letter",
                )
                router_client = _client(
                    settings,
                    store,
                    model=settings.yandex_ai_router_model,
                    reasoning_effort=settings.yandex_ai_router_reasoning_effort,
                    operation="cover_letter_routing",
                )
                queued = ApplicationAutomationService(session).prepare_for_account_id(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    include_stretch=not arguments.exclude_stretch,
                )
                result = CoverLetterService(session, client, router_client).prepare(
                    account_id=arguments.account_id,
                    direction_name=arguments.direction,
                    limit=arguments.limit,
                    vacancy_hh_id=arguments.vacancy_id,
                    include_stretch=not arguments.exclude_stretch,
                )
        finally:
            database.close()

        print(f"Новых заданий в очереди: {queued.created}. Ранее созданных: {queued.existing}.")
        labels = {
            "generated": "создано",
            "adapted": "исправлено лёгкой моделью",
            "reused": "выбрано готовое письмо",
            "existing": "уже готово",
            "failed": "ошибка",
            "blocked": "пропущено без нового обращения к модели",
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
    except (CodexCliError, LookupError, RuntimeError, ValueError, YandexAIError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПодготовка писем отменена. На hh.ru ничего не отправлено.", file=sys.stderr)
        return 130


def _client(
    settings: Settings,
    store: WindowsYandexAICredentialStore,
    *,
    model: str | None = None,
    reasoning_effort: str = "high",
    operation: str = "unspecified",
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
        model or credentials.model,
        settings.yandex_ai_base_url,
        settings.yandex_ai_timeout_seconds,
        reasoning_effort=reasoning_effort,
        journal=OperationJournal(settings.data_dir),
        operation=operation,
        connect_ip=(
            str(settings.yandex_ai_host_ip) if settings.yandex_ai_host_ip is not None else None
        ),
        source_ip=(
            str(settings.yandex_ai_source_ip) if settings.yandex_ai_source_ip is not None else None
        ),
    )


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
