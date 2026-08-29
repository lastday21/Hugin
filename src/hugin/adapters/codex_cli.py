from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal


class CodexCliError(RuntimeError):
    pass


def default_codex_cli_path() -> Path:
    if local_app_data := os.getenv("LOCALAPPDATA"):
        return Path(local_app_data) / "Hugin" / "codex-cli" / "node_modules" / ".bin" / "codex.cmd"
    return Path.home() / ".local" / "share" / "hugin" / "codex-cli" / "codex"


def find_codex_cli(configured_path: Path | None = None) -> Path:
    if configured_path is not None:
        path = configured_path.expanduser()
        if path.is_file():
            return path
        raise LookupError(f"Программа создания писем не найдена: {path}")

    local_path = default_codex_cli_path()
    if local_path.is_file():
        return local_path
    if installed := shutil.which("codex"):
        return Path(installed)
    raise LookupError(
        "Программа создания писем не установлена. Установите Codex и войдите через ChatGPT"
    )


class CodexCliClient:
    def __init__(
        self,
        executable: Path,
        runtime_dir: Path,
        *,
        model: str = "gpt-5.6-terra",
        reasoning_effort: str = "low",
        timeout_seconds: int = 180,
        journal: OperationJournal | None = None,
        operation: str = "cover_letter",
    ) -> None:
        self._executable = executable
        self._runtime_dir = runtime_dir
        self._model = model.strip()
        self._reasoning_effort = reasoning_effort.strip()
        self._timeout_seconds = timeout_seconds
        self._journal = journal
        self._operation = operation.strip() or "cover_letter"
        if not self._model:
            raise ValueError("Не указана модель для создания писем")
        if self._reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("Режим обработки должен быть low, medium или high")
        if not 30 <= timeout_seconds <= 300:
            raise ValueError("Время ожидания письма должно быть от 30 до 300 секунд")

    @property
    def model_name(self) -> str:
        return f"codex:{self._model}"

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        if self._operation == "recruiter_reply":
            result_name = "итоговый ответ работодателю"
        elif self._operation == "recruiter_reply_requirement":
            result_name = "решение REPLY_REQUIRED или NO_REPLY_REQUIRED"
        else:
            result_name = "итоговое сопроводительное письмо"
        prompt = (
            "Выполни только задачу создания текста. Не запускай команды и не читай файлы: "
            "все нужные данные уже приведены ниже.\n\n"
            f"<rules>\n{system_prompt}\n</rules>\n\n"
            f"<task>\n{user_prompt}\n</task>\n\n"
            f"Верни только {result_name} без пояснений и разметки."
        )
        command = [
            str(self._executable),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-C",
            str(self._runtime_dir),
            "-m",
            self._model,
            "-c",
            f'model_reasoning_effort="{self._reasoning_effort}"',
            "-",
        ]
        run = (
            self._journal.start(
                "codex_cli",
                "model.complete",
                operation=self._operation,
                model=self._model,
                model_calls=1,
                input_characters=len(system_prompt) + len(user_prompt),
                system_prompt_characters=len(system_prompt),
                user_prompt_characters=len(user_prompt),
                request_characters=len(prompt),
            )
            if self._journal is not None
            else None
        )
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("CODEX_API_KEY", None)
        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            failure = CodexCliError("Истекло время ожидания сопроводительного письма")
            if run is not None:
                run.fail(failure)
            raise failure from error
        except OSError as error:
            failure = CodexCliError("Не удалось запустить создание сопроводительного письма")
            if run is not None:
                run.fail(failure)
            raise failure from error

        if result.returncode != 0:
            detail = self._safe_error(result.stderr or result.stdout)
            failure = CodexCliError(
                "Создание сопроводительного письма завершилось ошибкой"
                + (f": {detail}" if detail else "")
            )
            if run is not None:
                run.fail(failure, return_code=result.returncode)
            raise failure
        text, usage = self._parse_output(result.stdout)
        if not text:
            failure = CodexCliError("Программа создания писем вернула пустой ответ")
            if run is not None:
                run.fail(failure)
            raise failure
        if run is not None:
            run.succeed(
                output_characters=len(text),
                token_usage_available=bool(usage),
                **usage,
            )
        return text

    @classmethod
    def _parse_output(cls, value: str) -> tuple[str, dict[str, int]]:
        messages: list[str] = []
        usage: dict[str, int] = {}
        parsed_event = False
        for line in value.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_event = True
            if event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        messages.append(text.strip())
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage")
                if isinstance(raw_usage, dict):
                    usage = cls._token_usage(raw_usage)
        if not parsed_event:
            return value.strip(), {}
        return (messages[-1] if messages else ""), usage

    @staticmethod
    def _token_usage(raw_usage: dict[str, object]) -> dict[str, int]:
        def non_negative_int(value: object) -> int | None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        input_tokens = non_negative_int(raw_usage.get("input_tokens"))
        output_tokens = non_negative_int(raw_usage.get("output_tokens"))
        cached_input_tokens = non_negative_int(raw_usage.get("cached_input_tokens"))
        reasoning_tokens = non_negative_int(raw_usage.get("reasoning_tokens"))

        input_details = raw_usage.get("input_tokens_details")
        if cached_input_tokens is None and isinstance(input_details, dict):
            cached_input_tokens = non_negative_int(input_details.get("cached_tokens"))
        output_details = raw_usage.get("output_tokens_details")
        if reasoning_tokens is None and isinstance(output_details, dict):
            reasoning_tokens = non_negative_int(output_details.get("reasoning_tokens"))

        total_tokens = non_negative_int(raw_usage.get("total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        values = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
        }
        return {key: value for key, value in values.items() if value is not None}

    @staticmethod
    def _safe_error(value: str) -> str:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return (lines[-1] if lines else "")[:500]


def configured_codex_cli_client(
    settings: Settings,
    *,
    operation: str = "cover_letter",
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> CodexCliClient:
    return CodexCliClient(
        find_codex_cli(settings.codex_cli_path),
        settings.data_dir / "codex-letter-runtime",
        model=model or settings.codex_letter_model,
        reasoning_effort=settings.codex_letter_reasoning_effort,
        timeout_seconds=timeout_seconds or settings.codex_letter_timeout_seconds,
        journal=OperationJournal(settings.data_dir),
        operation=operation,
    )
