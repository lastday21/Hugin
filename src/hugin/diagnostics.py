from __future__ import annotations

import json
import os
import re
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, TypedDict
from uuid import uuid4

_FILE_NAME = re.compile(r"^hugin-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|secret|session[_-]?key|token)",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{5,20}:[A-Za-z0-9_-]{20,100}\b")
_AUTHORIZATION = re.compile(r"\b(?:Api-Key|Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)\b(\s*[:=]\s*)([^\s,;]+)"
)
_URL_SECRET = re.compile(r"(?i)([?&](?:code|key|password|secret|start|token)=)[^&#\s]+")
_MAX_TEXT = 8_000
_MAX_ITEMS = 100
_WRITE_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_label(value: str, *, maximum: int) -> str:
    selected = " ".join(value.strip().split())
    if not selected:
        raise ValueError("Название события журнала не может быть пустым")
    return selected[:maximum]


def _redact_text(value: str) -> str:
    selected = _TELEGRAM_TOKEN.sub("***", value)
    selected = _AUTHORIZATION.sub("***", selected)
    selected = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}***",
        selected,
    )
    selected = _URL_SECRET.sub(lambda match: f"{match.group(1)}***", selected)
    return selected[:_MAX_TEXT]


def _safe_value(value: object, *, key: str = "", depth: int = 0) -> object:
    if _SENSITIVE_KEY.search(key):
        return "***"
    if depth >= 6:
        return "<ограничение глубины>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return _redact_text(str(value))
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        selected: dict[str, object] = {}
        for index, (nested_key, nested_value) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                selected["_truncated"] = True
                break
            label = str(nested_key)[:128]
            selected[label] = _safe_value(nested_value, key=label, depth=depth + 1)
        return selected
    if isinstance(value, Iterable):
        selected_items: list[object] = []
        for index, item in enumerate(value):
            if index >= _MAX_ITEMS:
                selected_items.append("<список сокращён>")
                break
            selected_items.append(_safe_value(item, depth=depth + 1))
        return selected_items
    return _redact_text(repr(value))


class ErrorDetails(TypedDict):
    error_type: str
    error_message: str
    traceback: str


def error_details(error: BaseException) -> ErrorDetails:
    trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return {
        "error_type": type(error).__name__,
        "error_message": _redact_text(str(error) or type(error).__name__),
        "traceback": _redact_text(trace),
    }


class OperationJournal:
    def __init__(
        self,
        data_dir: Path,
        *,
        retention_days: int = 90,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        if retention_days < 1:
            raise ValueError("Срок хранения журнала должен быть положительным")
        self._log_dir = data_dir / "logs"
        self._retention_days = retention_days
        self._clock = clock or _utc_now
        self._timer = timer or monotonic
        self._last_pruned_on: date | None = None

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    def start(
        self,
        component: str,
        event: str,
        *,
        run_id: str | None = None,
        level: str = "INFO",
        **details: object,
    ) -> JournalRun:
        selected_run_id = run_id or uuid4().hex
        started_at = self._timer()
        self.record(
            component,
            event,
            status="started",
            level=level,
            run_id=selected_run_id,
            **details,
        )
        return JournalRun(
            self,
            component,
            event,
            selected_run_id,
            started_at,
        )

    def record(
        self,
        component: str,
        event: str,
        *,
        status: str,
        level: str = "INFO",
        run_id: str | None = None,
        **details: object,
    ) -> bool:
        timestamp = self._clock().astimezone(UTC)
        payload: dict[str, object] = {
            "timestamp": timestamp.isoformat(),
            "level": _clean_label(level.upper(), maximum=16),
            "component": _clean_label(component, maximum=64),
            "event": _clean_label(event, maximum=96),
            "status": _clean_label(status, maximum=32),
            "process_id": os.getpid(),
            "thread": threading.current_thread().name[:128],
        }
        if run_id:
            payload["run_id"] = _clean_label(run_id, maximum=64)
        safe_details = _safe_value(details)
        if isinstance(safe_details, dict) and safe_details:
            payload["details"] = safe_details
        try:
            self._append(timestamp, payload)
            self._prune_once(timestamp.date())
        except OSError:
            return False
        return True

    def prune(self, retention_days: int | None = None, *, now: datetime | None = None) -> int:
        selected_retention = retention_days or self._retention_days
        if selected_retention < 1:
            raise ValueError("Срок хранения журнала должен быть положительным")
        if not self._log_dir.is_dir():
            return 0
        threshold = (now or self._clock()).astimezone(UTC).date() - timedelta(
            days=selected_retention
        )
        removed = 0
        resolved_log_dir = self._log_dir.resolve()
        for path in self._log_dir.iterdir():
            matched = _FILE_NAME.fullmatch(path.name)
            if not matched or not path.is_file():
                continue
            file_day = date.fromisoformat(matched.group("day"))
            if file_day >= threshold:
                continue
            resolved_path = path.resolve()
            if resolved_path.parent != resolved_log_dir:
                continue
            resolved_path.unlink()
            removed += 1
        return removed

    def entries(
        self,
        *,
        since: datetime | None = None,
        component: str | None = None,
        status: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not self._log_dir.is_dir():
            return
        selected_since = since.astimezone(UTC) if since is not None else None
        for path in sorted(self._log_dir.glob("hugin-*.jsonl")):
            if not _FILE_NAME.fullmatch(path.name):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if component is not None and entry.get("component") != component:
                    continue
                if status is not None and entry.get("status") != status:
                    continue
                if selected_since is not None:
                    timestamp = entry.get("timestamp")
                    if not isinstance(timestamp, str):
                        continue
                    try:
                        occurred_at = datetime.fromisoformat(timestamp).astimezone(UTC)
                    except ValueError:
                        continue
                    if occurred_at < selected_since:
                        continue
                yield entry

    def _append(self, timestamp: datetime, payload: dict[str, object]) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / f"hugin-{timestamp.date().isoformat()}.jsonl"
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with _WRITE_LOCK, path.open("a", encoding="utf-8", newline="\n") as journal:
            journal.write(serialized)
            journal.write("\n")
        if os.name != "nt":
            path.chmod(0o600)

    def _prune_once(self, today: date) -> None:
        if self._last_pruned_on == today:
            return
        self.prune(now=datetime.combine(today, datetime.min.time(), tzinfo=UTC))
        self._last_pruned_on = today


class JournalRun:
    def __init__(
        self,
        journal: OperationJournal,
        component: str,
        event: str,
        run_id: str,
        started_at: float,
    ) -> None:
        self._journal = journal
        self._component = component
        self._event = event
        self._run_id = run_id
        self._started_at = started_at
        self._finished = False

    @property
    def run_id(self) -> str:
        return self._run_id

    def succeed(self, **details: object) -> None:
        self._finish("completed", "INFO", details)

    def skip(self, **details: object) -> None:
        self._finish("skipped", "INFO", details)

    def block(self, **details: object) -> None:
        self._finish("blocked", "WARNING", details)

    def fail(self, error: BaseException, **details: object) -> None:
        selected = {**details, **error_details(error)}
        self._finish("failed", "ERROR", selected)

    def _finish(self, status: str, level: str, details: Mapping[str, object]) -> None:
        if self._finished:
            return
        self._finished = True
        duration_ms = max(0, round((self._journal._timer() - self._started_at) * 1000))
        self._journal.record(
            self._component,
            self._event,
            status=status,
            level=level,
            run_id=self._run_id,
            duration_ms=duration_ms,
            **details,
        )
