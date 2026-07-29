# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hugin.core.settings import get_settings
from hugin.diagnostics import OperationJournal


def positive_integer(value: str) -> int:
    selected = int(value)
    if selected < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hugin-journal",
        description="Просмотр местного журнала работы Hugin без секретов",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="показать последние записи")
    _add_filters(show)
    show.add_argument("--limit", type=positive_integer, default=100)
    show.add_argument(
        "--details",
        action="store_true",
        help="печатать полную запись в формате JSON",
    )

    summary = subparsers.add_parser("summary", help="сводка работы и ошибок")
    _add_filters(summary)

    export = subparsers.add_parser("export", help="выгрузить записи для анализа")
    _add_filters(export)
    export.add_argument("path", type=Path)
    export.add_argument(
        "--replace",
        action="store_true",
        help="заменить существующий файл",
    )
    return parser


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hours", type=positive_integer, default=24)
    parser.add_argument("--component")
    parser.add_argument("--status")


def _entries(arguments: argparse.Namespace) -> list[dict[str, Any]]:
    journal = OperationJournal(get_settings().data_dir)
    since = datetime.now(UTC) - timedelta(hours=arguments.hours)
    return list(
        journal.entries(
            since=since,
            component=arguments.component,
            status=arguments.status,
        )
    )


def _show(entries: list[dict[str, Any]], *, limit: int, details: bool) -> None:
    selected = entries[-limit:]
    if not selected:
        print("За выбранный период записей нет.")
        return
    for entry in selected:
        if details:
            print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            continue
        details_value = entry.get("details")
        details_map = details_value if isinstance(details_value, dict) else {}
        duration = details_map.get("duration_ms")
        error_type = details_map.get("error_type")
        error_message = details_map.get("error_message")
        suffixes: list[str] = []
        if isinstance(duration, int | float):
            suffixes.append(f"{duration} мс")
        if isinstance(error_type, str):
            suffixes.append(
                f"{error_type}: {error_message}" if isinstance(error_message, str) else error_type
            )
        suffix = f" | {' | '.join(suffixes)}" if suffixes else ""
        print(
            f"{entry.get('timestamp', '?')} | {entry.get('level', '?')} | "
            f"{entry.get('component', '?')} | {entry.get('event', '?')} | "
            f"{entry.get('status', '?')}{suffix}"
        )


def _summary(entries: list[dict[str, Any]], *, hours: int) -> None:
    statuses = Counter(str(entry.get("status", "unknown")) for entry in entries)
    events = Counter(
        f"{entry.get('component', '?')}.{entry.get('event', '?')}" for entry in entries
    )
    failures = [entry for entry in entries if entry.get("status") in {"failed", "blocked"}]
    print(f"Период: последние {hours} ч.")
    print(f"Всего записей: {len(entries)}")
    print(
        "Состояния: "
        + (", ".join(f"{key} — {value}" for key, value in statuses.most_common()) or "нет")
    )
    print("Самые частые операции:")
    for event, count in events.most_common(10):
        print(f"  {event}: {count}")
    print(f"Ошибки и блокировки: {len(failures)}")
    for entry in failures[-10:]:
        details_value = entry.get("details")
        details = details_value if isinstance(details_value, dict) else {}
        reason = details.get("error_message") or details.get("result_message") or ""
        print(
            f"  {entry.get('timestamp', '?')} | "
            f"{entry.get('component', '?')}.{entry.get('event', '?')} | "
            f"{entry.get('status', '?')} | {reason}"
        )


def _export(entries: list[dict[str, Any]], path: Path, *, replace: bool) -> None:
    selected_path = path.expanduser().resolve()
    if selected_path.exists() and not replace:
        raise FileExistsError(
            f"Файл уже существует: {selected_path}. Добавьте --replace для замены."
        )
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        f"{json.dumps(entry, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}\n"
        for entry in entries
    )
    selected_path.write_text(text, encoding="utf-8")
    print(f"Выгружено записей: {len(entries)}")
    print(f"Файл: {selected_path}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    entries = _entries(arguments)
    if arguments.command == "show":
        _show(entries, limit=arguments.limit, details=arguments.details)
    elif arguments.command == "summary":
        _summary(entries, hours=arguments.hours)
    else:
        _export(entries, arguments.path, replace=arguments.replace)
    return 0
