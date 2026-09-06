from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from hugin.core.settings import get_settings
from hugin.database import create_database
from hugin.database.models import VacancyChangeModel
from hugin.services.decision_evidence import replay_ranking
from hugin.services.operation_trace import OperationTraceService


def _since(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Укажите дату и время в формате ISO 8601") from error
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("Укажите часовой пояс, например +05:00")
    return result


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Диагностика Hugin без внешних действий")
    commands = parser.add_subparsers(dest="command", required=True)
    timeline = commands.add_parser("timeline", help="время работы, ожидания и фоновых заданий")
    timeline.add_argument("--since", type=_since, required=True)
    timeline.add_argument("--until", type=_since)
    timeline.add_argument("--output", type=Path, required=True)
    timeline.add_argument("--local-only", action="store_true", help="не читать журнал сервера")
    audit = commands.add_parser("audit", help="измерить полноту сохранённой истории")
    audit.add_argument("--limit", type=int, default=20, choices=range(1, 1001), metavar="1..1000")
    audit.add_argument("--since", type=_since)
    audit.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check", help="проверить согласованность местных данных")
    check.add_argument("--output", type=Path)
    check.add_argument(
        "--require-stopped",
        action="store_true",
        help="требовать остановки всех автоматических отправок",
    )
    trace = commands.add_parser("trace", help="собрать историю отклика")
    trace.add_argument("application_id", type=int)
    trace.add_argument("--output", type=Path, required=True)
    trace.add_argument(
        "--private", action="store_true", help="сохранить полные исходные тексты местно"
    )
    replay = commands.add_parser("replay", help="повторить сохранённое решение отбора")
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-id", type=int)
    source.add_argument("--file", type=Path)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "replay" and args.file:
        report = replay_ranking(json.loads(args.file.read_text(encoding="utf-8")))
    else:
        settings = get_settings()
        database = create_database(settings)
        try:
            with database.sessions() as session:
                service = OperationTraceService(session, data_dir=settings.data_dir)
                if args.command == "timeline":
                    end = args.until or datetime.now(UTC)
                    server_records = []
                    server_issues = []
                    server_checked = False
                    if not args.local_only:
                        try:
                            with httpx.Client(trust_env=False, timeout=10) as client:
                                response = client.get(
                                    settings.desktop_api_url.rstrip("/")
                                    + "/api/diagnostics/journal",
                                    params={
                                        "since": args.since.isoformat(),
                                        "until": end.isoformat(),
                                    },
                                )
                                response.raise_for_status()
                                payload = response.json()
                            server_records = payload["records"]
                            server_issues = payload["journal_read_issues"]
                            server_checked = True
                        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                            server_issues = [
                                {
                                    "reason": "server_journal_unavailable",
                                    "error_type": type(error).__name__,
                                }
                            ]
                    report = service.timeline(
                        since=args.since, until=end, additional_records=server_records
                    )
                    report["server_journal_checked"] = server_checked
                    report["server_journal_issues"] = server_issues
                    report["scope"] = "desktop_and_server" if server_checked else "local_only"
                elif args.command == "audit":
                    report = service.audit(limit=args.limit, since=args.since)
                elif args.command == "trace":
                    report = service.application(args.application_id, private=args.private)
                elif args.command == "check":
                    report = service.check()
                else:
                    row = session.get(VacancyChangeModel, args.evidence_id)
                    if row is None or row.event_type != "RULES_EVALUATED":
                        raise LookupError("Сохранённое решение не найдено")
                    report = replay_ranking(row.changes)
        finally:
            database.close()
    if args.command == "check" and args.require_stopped:
        report["stopped_required"] = True
        report["ok"] = report["ok"] and report["automatic_sending_stopped"]
    if args.output:
        write_report(args.output, report)
        print(f"Сохранено: {args.output}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", report.get("matches", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
