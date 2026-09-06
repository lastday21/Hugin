from __future__ import annotations

import json
import os
import re
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
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
    r"(?i)([\"']?\b(?:api[_-]?key|authorization|cookie|password|secret|token)\b[\"']?)"
    r"(\s*[:=]\s*)(?:\"(?:\\.|[^\"\\\n])*(?:\"|(?=\n|$))|"
    r"'(?:\\.|[^'\\\n])*(?:'|(?=\n|$))|[^\s,;&#]+)"
)
_URL_SECRET = re.compile(
    r"(?i)([?&](?:code|key|password|secret|start|token|api[_-]?key|"
    r"(?:access|refresh|id)[_-]?token|client[_-]?secret|session[_-]?(?:id|key))=)[^&#\s]+"
)
_URL_CREDENTIALS = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]*:[^/\s]+@")
_MAX_TEXT = 8_000
_MAX_ITEMS = 100
_TOKEN_COUNTS = frozenset(
    {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"}
)
_SOURCE_TEXT_KEYS = frozenset(
    {
        "error_message",
        "traceback",
        "result_message",
        "text",
        "body",
        "content",
        "description",
        "title",
        "stdout",
        "stderr",
        "prompt",
        "user_prompt",
        "system_prompt",
        "response_text",
        "answer",
        "question",
        "resume_content",
        "letter_text",
        "cover_letter",
    }
)
_WRITE_LOCK = threading.RLock()
_OPERATION: ContextVar[dict[str, object] | None] = ContextVar("journal_operation", default=None)


@contextmanager
def operation_context(**identifiers: object) -> Iterator[None]:
    token = _OPERATION.set({**(_OPERATION.get() or {}), **identifiers})
    try:
        yield
    finally:
        _OPERATION.reset(token)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean_label(value: str, *, maximum: int) -> str:
    selected = " ".join(value.strip().split())
    if not selected:
        raise ValueError("Название события журнала не может быть пустым")
    return selected[:maximum]


def _redact_text(value: str) -> str:
    selected = _URL_CREDENTIALS.sub(r"\1***@", value)
    selected = _TELEGRAM_TOKEN.sub("***", selected)
    selected = _AUTHORIZATION.sub("***", selected)
    selected = _URL_SECRET.sub(lambda match: f"{match.group(1)}***", selected)
    selected = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}***",
        selected,
    )
    return selected[:_MAX_TEXT]


def _safe_value(value: object, *, key: str = "", depth: int = 0) -> object:
    if key in _SOURCE_TEXT_KEYS and value is not None:
        from hugin.services.decision_evidence import fingerprint

        text = _redact_text(str(value))
        return {"characters": len(text), "sha256": fingerprint(text)}
    if key == "token_usage_available" and isinstance(value, bool):
        return value
    if (
        key in _TOKEN_COUNTS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value
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

    def save_evidence(self, run_id: str, stage: str, payload: Mapping[str, object]) -> bool:
        from hugin.services.decision_evidence import fingerprint, source_fingerprint

        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", run_id) is None or stage not in {
            "request",
            "response",
            "failure",
        }:
            raise ValueError("Invalid evidence identifier")
        record = {
            "schema_version": 1,
            "run_id": run_id,
            "stage": stage,
            "captured_at": self._clock().astimezone(UTC).isoformat(),
            "source_sha256": source_fingerprint(),
            "payload": dict(payload),
        }
        record["sha256"] = fingerprint(record)
        directory = self._log_dir.parent / "evidence" / "models"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                directory / f"{run_id}-{stage}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
        except OSError as error:
            self.record(
                "diagnostics",
                "evidence.write",
                status="failed",
                run_id=run_id,
                stage=stage,
                error_type=type(error).__name__,
            )
            return False
        return True

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
        details = {**(_OPERATION.get() or {}), **details}
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
            details,
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
        from hugin.services.decision_evidence import source_fingerprint

        timestamp = self._clock().astimezone(UTC)
        payload: dict[str, object] = {
            "schema_version": 1,
            "source_sha256": source_fingerprint(),
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
        safe_details = _safe_value({**(_OPERATION.get() or {}), **details})
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
            try:
                file_day = date.fromisoformat(matched.group("day"))
            except ValueError:
                continue
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
        issues: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not self._log_dir.is_dir():
            return
        selected_since = since.astimezone(UTC) if since is not None else None
        for path in sorted(self._log_dir.glob("hugin-*.jsonl")):
            if not _FILE_NAME.fullmatch(path.name):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                if issues is not None:
                    issues.append({"file": path.name, "reason": "unreadable"})
                continue
            for line_number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    if issues is not None:
                        issues.append(
                            {"file": path.name, "line": line_number, "reason": "invalid_json"}
                        )
                    continue
                if not isinstance(entry, dict):
                    if issues is not None:
                        issues.append(
                            {"file": path.name, "line": line_number, "reason": "invalid_record"}
                        )
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
                    except (ValueError, OverflowError):
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
        directory = self._log_dir.parent / "evidence" / "models"
        if directory.is_dir():
            threshold = datetime.combine(today, datetime.min.time(), tzinfo=UTC) - timedelta(
                days=self._retention_days
            )
            resolved = directory.resolve()
            for path in directory.iterdir():
                if (
                    re.fullmatch(
                        r"[a-zA-Z0-9_-]{1,64}-(?:request|response|failure)\.json", path.name
                    )
                    is None
                ):
                    continue
                if (
                    path.is_file()
                    and path.resolve().parent == resolved
                    and path.stat().st_mtime < threshold.timestamp()
                ):
                    path.unlink()
        self._last_pruned_on = today


class JournalRun:
    def __init__(
        self,
        journal: OperationJournal,
        component: str,
        event: str,
        run_id: str,
        started_at: float,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self._journal = journal
        self._component = component
        self._event = event
        self._run_id = run_id
        self._started_at = started_at
        self._finished = False
        self._details = {
            key: value for key, value in (details or {}).items() if key != "model_calls"
        }

    @property
    def run_id(self) -> str:
        return self._run_id

    @contextmanager
    def step(self, name: str, **details: object) -> Iterator[JournalRun]:
        child = self._journal.start(
            self._component,
            name,
            run_id=None,
            level="INFO",
            **{
                **self._details,
                "parent_run_id": self._run_id,
                "step_name": name,
                "required_steps": [],
                **details,
            },
        )
        try:
            identifiers = {
                key: value
                for key, value in self._details.items()
                if key
                in {
                    "account_id",
                    "application_id",
                    "task_id",
                    "attempt_number",
                    "job_key",
                    "job_kind",
                }
            }
            with operation_context(**identifiers, parent_run_id=child.run_id):
                yield child
        except Exception as error:
            child.fail(error)
            raise
        else:
            child.succeed()

    def save_evidence(self, stage: str, **payload: object) -> bool:
        saved = self._journal.save_evidence(self._run_id, stage, payload)
        self._details[f"{stage}_evidence_saved"] = saved
        return saved

    def succeed(self, **details: object) -> None:
        self._finish("completed", "INFO", details)

    def skip(self, **details: object) -> None:
        self._finish("skipped", "INFO", details)

    def block(self, **details: object) -> None:
        self._finish("blocked", "WARNING", details)

    def fail(self, error: BaseException, **details: object) -> None:
        if self._finished:
            return
        selected = {**details, **error_details(error)}
        self.save_evidence("failure", **selected)
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
            **{**self._details, **details, "duration_ms": duration_ms},
        )
