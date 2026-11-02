from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from hugin.core.settings import Settings
from hugin.database import create_database
from hugin.database.models import SystemStateModel
from hugin.diagnostics import OperationJournal
from hugin.domain.tasks import SystemState
from hugin.services.decision_evidence import source_fingerprint

type AgentRequest = Callable[[list[str]], subprocess.CompletedProcess[str]]


class CaptchaAgentDispatcher:
    """Queue one request and open its desktop chat for browser-enabled execution."""

    def __init__(
        self,
        settings: Settings,
        *,
        journal: OperationJournal | None = None,
        agent_request: AgentRequest | None = None,
    ) -> None:
        self._settings = settings
        self._journal = journal or OperationJournal(settings.data_dir)
        self._agent_request = agent_request or self._run_agent

    @staticmethod
    def _run_agent(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )

    @staticmethod
    def _queued_message(output: str, thread_id: str) -> str | None:
        match = re.fullmatch(r"Queued message ([\da-f-]+) for thread ([\da-f-]+)\.", output.strip())
        if match is None or match[2] != thread_id:
            return None
        try:
            return str(UUID(match[1]))
        except ValueError:
            return None

    def dispatch_current(self) -> bool:
        if not self._settings.captcha_agent_thread_id:
            return False
        database = create_database(self._settings)
        try:
            with database.sessions() as session:
                row = session.execute(
                    select(
                        SystemStateModel.state,
                        SystemStateModel.recovery_state,
                        SystemStateModel.captcha_case_id,
                    ).where(SystemStateModel.id == 1)
                ).one_or_none()
        finally:
            database.close()
        if (
            row is None
            or row.state is not SystemState.CAPTCHA_REQUIRED
            or row.recovery_state is SystemState.PAUSED
            or row.captcha_case_id is None
        ):
            return False
        case_id = row.captcha_case_id
        thread_id = self._settings.captcha_agent_thread_id
        key = hashlib.sha256(f"{thread_id}:{case_id}".encode()).hexdigest()
        directory = self._settings.data_dir / "captcha-agent"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{key}.json"
        record: dict[str, object] = {
            "case_id": case_id,
            "thread_id": thread_id,
            "source_sha256": source_fingerprint(),
            "status": "dispatching",
            "requested_at": datetime.now(UTC).isoformat(),
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
        }
        try:
            with path.open("x", encoding="utf8") as stream:
                json.dump(record, stream, ensure_ascii=False)
        except FileExistsError:
            return False
        run = self._journal.start("captcha", "agent.request", case_id=case_id, thread_id=thread_id)
        try:
            executable = self._settings.captcha_agent_codex_path
            if executable is None or not executable.is_file():
                found = shutil.which("codex")
                if found is None:
                    raise FileNotFoundError("Не найдена программа Codex для запроса агента")
                executable = Path(found)
            command = [
                str(executable),
                "queue",
                "--thread",
                thread_id,
                "--model",
                "gpt-6.1-sol",
                "-c",
                'model_reasoning_effort="high"',
                "--message",
                self._prompt(case_id),
            ]
            result = self._agent_request(command)
            output_path = path.with_suffix(".stdout.txt")
            output_path.write_text(result.stdout, encoding="utf8")
            path.with_suffix(".stderr.txt").write_text(result.stderr, encoding="utf8")
            record.update(output_path=str(output_path), exit_code=result.returncode)
            message_id = self._queued_message(result.stdout, thread_id)
            if result.returncode != 0 or message_id is None:
                record["status"] = "unconfirmed"
                raise RuntimeError("Codex не подтвердил постановку запроса; повтор не выполняется")
            record.update(message_id=message_id, status="desktop_failed")
            # Opening the existing chat registers the browser-capable desktop executor.
            # A separate `exec resume` turn lacks that registration and is denied browser access.
            open_url = getattr(os, "startfile", None)
            if open_url is None:
                raise RuntimeError("Запуск чата Codex поддерживается только в Windows")
            open_url(f"codex://threads/{thread_id}")
            record["status"] = "desktop_requested"
            run.succeed(message_id=message_id, state="desktop_requested")
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            if isinstance(error, subprocess.TimeoutExpired):
                for suffix, output in (
                    (".stdout.txt", error.stdout),
                    (".stderr.txt", error.stderr),
                ):
                    partial = (
                        output.decode("utf8", errors="replace")
                        if isinstance(output, bytes)
                        else output
                    )
                    path.with_suffix(suffix).write_text(partial or "", encoding="utf8")
                record["output_path"] = str(path.with_suffix(".stdout.txt"))
            if record["status"] == "dispatching":
                record["status"] = (
                    "unknown" if isinstance(error, subprocess.TimeoutExpired) else "failed"
                )
            record["error_type"] = type(error).__name__
            run.fail(error)
            return False
        finally:
            record["finished_at"] = datetime.now(UTC).isoformat()
            temporary = directory / f".{uuid4().hex}.json"
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf8")
            os.replace(temporary, path)
        return True

    @staticmethod
    def _prompt(case_id: str) -> str:
        return (
            f"HUGIN_CAPTCHA_EVENT: case_id={case_id}. "
            "Hugin сохранил новую CAPTCHA_REQUIRED во время разрешённой работы и отправил этот "
            "одиночный событийный запрос. Это не периодическое наблюдение. "
            "Сначала проверь актуальное состояние Hugin и номер случая; если проверка уже снята, "
            "не предпринимай внешних действий. Прочитай действующие AGENTS.md и только связанные "
            "с этим случаем последние записи текущего проекта. Проверь по ним действующее "
            "разрешение пользователя на автономное прохождение CAPTCHA hh.ru; если разрешение "
            "не подтверждено, остановись и запроси его. Через штатную кнопку Hugin открой текущую "
            "проверку в том же браузере, проверь точный домен hh.ru и пройди CAPTCHA средствами "
            "Computer Use. Для текста можно выбрать English; не более трёх попыток на этот случай. "
            "Не обходи ограничения инструмента, не создавай искусственную CAPTCHA и не придумывай "
            "неизвестные данные. Не вмешивайся в занятое ручное управление. "
            "После принятия проверки "
            "дождись штатного восстановления, проверь свежие состояние и журнал. Не ставь RUNNING "
            "вручную и не повторяй UNKNOWN_RESULT. Сохрани ручную паузу, дневной лимит, качество, "
            "политику автоответов и игровые ограничения. Не запускай дополнительные отклики, "
            "аудит, публикации или исправления. Сохрани номер случая/результат локально, сообщи "
            "только о восстановлении либо необходимом действии пользователя; одинаковое сообщение "
            "на один случай не повторяй. Периодические расписания не создавай."
        )
