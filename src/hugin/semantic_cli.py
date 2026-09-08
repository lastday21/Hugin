from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from hugin.core.settings import get_settings
from hugin.database import create_database
from hugin.diagnostic_cli import write_report
from hugin.repositories.directions import DirectionRepository
from hugin.repositories.vacancies import VacancyRepository
from hugin.services.semantic_processing import SemanticSelectionProcessor
from hugin.services.semantic_results import read_selection
from hugin.services.semantic_snapshot import selection_snapshot


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Смысловой отбор сохранённой вакансии Hugin")
    parser.add_argument(
        "command",
        choices=("inspect", "analyze"),
        help="inspect читает сохранённый разбор; analyze обращается к модели и сохраняет решение",
    )
    parser.add_argument("--account-id", type=int, default=1)
    parser.add_argument("--direction-id", type=int, required=True)
    parser.add_argument(
        "--vacancy-id", type=int, required=True, help="местный номер вакансии в базе Hugin"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="новый местный файл; содержит исходные тексты вакансии и профиля",
    )
    args = parser.parse_args(argv)
    if min(args.account_id, args.direction_id, args.vacancy_id) < 1:
        parser.error("Номера аккаунта, направления и вакансии должны быть положительными")
    if args.output.exists():
        parser.error("Файл результата уже существует; укажите новое имя")
    settings = get_settings()
    processing = None
    if args.command == "analyze":
        processing = SemanticSelectionProcessor(settings).process(
            args.account_id, args.direction_id, args.vacancy_id
        )
    database = create_database(settings)
    try:
        with database.sessions() as session:
            direction = DirectionRepository(session).get_for_account(
                args.account_id, args.direction_id
            )
            vacancy = VacancyRepository(session).get(args.vacancy_id)
            snapshot = selection_snapshot(session, direction, vacancy)
            result = read_selection(session, snapshot) if snapshot is not None else None
            report = {
                "account_id": args.account_id,
                "direction_id": args.direction_id,
                "vacancy_id": args.vacancy_id,
                "hh_id": vacancy.hh_id,
                "status": result.decision.status if result else "DISABLED",
                "evidence": result.evidence if result else None,
                "retry_due": result.due if result else False,
                "processing": asdict(processing) if processing else None,
                "hh_actions": 0,
            }
    finally:
        database.close()
    write_report(args.output, report)
    print(json.dumps({"status": report["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
