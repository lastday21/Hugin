from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request
from ipaddress import IPv4Address
from typing import Any

from hugin.diagnostics import OperationJournal

REASONING_EFFORTS = frozenset({"low", "medium", "high"})


class YandexAIError(RuntimeError):
    pass


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        connect_ip: str,
        source_address: tuple[str, int],
        **kwargs: Any,
    ) -> None:
        context = ssl.create_default_context()
        super().__init__(
            host,
            source_address=source_address,
            context=context,
            **kwargs,
        )
        self._connect_ip = connect_ip
        self._hugin_source_address = source_address
        self._hugin_context = context

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self._hugin_source_address,
        )
        self.sock = self._hugin_context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )


class _SourceAddressHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, source_ip: str, connect_ip: str | None = None) -> None:
        super().__init__()
        self._source_address = (str(IPv4Address(source_ip)), 0)
        self._connect_ip = str(IPv4Address(connect_ip)) if connect_ip else None

    def https_open(self, request: urllib.request.Request) -> Any:
        def connection(
            host: str,
            **kwargs: Any,
        ) -> http.client.HTTPSConnection:
            if self._connect_ip is not None:
                return _BoundHTTPSConnection(
                    host,
                    connect_ip=self._connect_ip,
                    source_address=self._source_address,
                    **kwargs,
                )
            return http.client.HTTPSConnection(
                host,
                source_address=self._source_address,
                **kwargs,
            )

        return self.do_open(connection, request)


class YandexAIClient:
    def __init__(
        self,
        api_key: str,
        folder_id: str,
        model: str = "yandexgpt/latest",
        base_url: str = "https://ai.api.cloud.yandex.net/v1",
        timeout_seconds: int = 120,
        temperature: float = 0.1,
        reasoning_effort: str = "high",
        journal: OperationJournal | None = None,
        operation: str = "unspecified",
        connect_ip: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._folder_id = folder_id.strip()
        self._model = model.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort.strip()
        self._journal = journal
        self._operation = operation.strip() or "unspecified"
        self._opener = (
            urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _SourceAddressHTTPSHandler(source_ip, connect_ip),
            )
            if source_ip
            else None
        )
        if connect_ip and not source_ip:
            raise ValueError("Для прямого адреса YandexGPT нужен исходящий сетевой адрес")
        if not self._api_key:
            raise ValueError("Не указан ключ Yandex AI Studio")
        if not self._folder_id:
            raise ValueError("Не указан идентификатор каталога Yandex Cloud")
        if not self._model:
            raise ValueError("Не указана модель YandexGPT")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("Время ожидания YandexGPT должно быть от 1 до 300 секунд")
        if not 0 <= temperature <= 2:
            raise ValueError("Температура YandexGPT должна быть от 0 до 2")
        if self._reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Режим обработки должен быть low, medium или high")

    @property
    def model_name(self) -> str:
        return self._model

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        body = {
            "model": self._model_uri(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": True,
            "temperature": self._temperature,
            "reasoning_effort": self._reasoning_effort,
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Api-Key {self._api_key}",
                "x-folder-id": self._folder_id,
                "x-project": self._folder_id,
                "OpenAI-Project": self._folder_id,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-data-logging-enabled": "false",
            },
            method="POST",
        )
        chunks: list[str] = []
        usage: dict[str, int] = {}
        run = (
            self._journal.start(
                "yandex_ai",
                "model.complete",
                operation=self._operation,
                model=self._model,
                model_calls=1,
                input_characters=len(system_prompt) + len(user_prompt),
            )
            if self._journal is not None
            else None
        )
        try:
            open_request = (
                self._opener.open
                if self._opener is not None
                else urllib.request.urlopen
            )
            with open_request(request, timeout=self._timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line.removeprefix("data:").strip()
                    if payload == "[DONE]":
                        break
                    parsed_usage = self._parse_usage(payload)
                    if parsed_usage:
                        usage = parsed_usage
                    chunk = self._parse_chunk(payload)
                    if chunk:
                        chunks.append(chunk)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            failure = YandexAIError(f"Yandex AI Studio вернул ошибку HTTP {error.code}: {detail}")
            if run is not None:
                run.fail(failure)
            raise failure from error
        except urllib.error.URLError as error:
            failure = YandexAIError(f"Yandex AI Studio недоступен: {error.reason}")
            if run is not None:
                run.fail(failure)
            raise failure from error
        except TimeoutError as error:
            failure = YandexAIError("Истекло время ожидания ответа YandexGPT")
            if run is not None:
                run.fail(failure)
            raise failure from error
        except OSError as error:
            failure = YandexAIError(f"Ошибка запроса к YandexGPT: {error}")
            if run is not None:
                run.fail(failure)
            raise failure from error

        result = "".join(chunks).strip()
        if not result:
            failure = YandexAIError("YandexGPT вернул пустой ответ")
            if run is not None:
                run.fail(failure)
            raise failure
        if run is not None:
            usage_details: dict[str, object] = {
                "usage_reported": bool(usage),
            }
            if usage:
                usage_details["usage_unit"] = "tokens"
            run.succeed(
                operation=self._operation,
                model=self._model,
                output_characters=len(result),
                **usage_details,
                **usage,
            )
        return result

    def _model_uri(self) -> str:
        if "://" in self._model:
            return self._model
        return f"gpt://{self._folder_id}/{self._model}"

    @staticmethod
    def _parse_chunk(payload: str) -> str:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ""
        delta = choices[0].get("delta")
        if isinstance(delta, dict):
            return str(delta.get("content") or "")
        message = choices[0].get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return ""

    @staticmethod
    def _parse_usage(payload: str) -> dict[str, int]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        raw = data.get("usage")
        if not isinstance(raw, dict):
            return {}
        aliases = {
            "prompt_units": ("prompt_tokens", "input_tokens", "inputTextTokens"),
            "completion_units": (
                "completion_tokens",
                "output_tokens",
                "completionTokens",
            ),
            "total_units": ("total_tokens", "totalTokens"),
        }
        result: dict[str, int] = {}
        for target, names in aliases.items():
            value = next((raw.get(name) for name in names if name in raw), None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[target] = value
        return result
