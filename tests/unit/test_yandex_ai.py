from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Self

import pytest

import hugin.adapters.yandex_ai as yandex_module
from hugin.adapters.yandex_ai import YandexAIClient, YandexAIError
from hugin.diagnostics import OperationJournal


class FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.fp: object | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._lines)


def test_yandex_client_streams_private_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, *, timeout: int) -> FakeResponse:
        assert timeout == 45
        captured.append(request)
        chunks = [
            {"choices": [{"delta": {"content": "Гото"}}]},
            {"choices": [{"delta": {"content": "во"}}]},
        ]
        return FakeResponse(
            [*(f"data: {json.dumps(chunk)}\n".encode() for chunk in chunks), b"data: [DONE]\n"]
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = YandexAIClient("secret-key", "folder-id", timeout_seconds=45)

    assert client.complete("Системное правило", "Пользовательский запрос") == "Готово"
    assert client.model_name == "yandexgpt/latest"
    request = captured[0]
    assert request.get_header("X-data-logging-enabled") == "false"
    assert request.get_header("Authorization") == "Api-Key secret-key"
    assert isinstance(request.data, bytes)
    body = json.loads(request.data.decode())
    assert body["model"] == "gpt://folder-id/yandexgpt/latest"
    assert body["stream"] is True
    assert body["temperature"] == 0.1
    assert body["messages"][1]["content"] == "Пользовательский запрос"


def test_yandex_client_uses_selected_source_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[urllib.request.Request, int]] = []
    handlers: list[urllib.request.BaseHandler] = []

    class FakeOpener:
        def open(
            self,
            request: urllib.request.Request,
            *,
            timeout: int,
        ) -> FakeResponse:
            opened.append((request, timeout))
            return FakeResponse([b'data: {"choices":[{"message":{"content":"ok"}}]}\n'])

    def build_opener(
        *selected_handlers: urllib.request.BaseHandler,
    ) -> FakeOpener:
        handlers.extend(selected_handlers)
        return FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(yandex_module, "usable_source_ipv4", lambda value: value)
    client = YandexAIClient(
        "key",
        "folder",
        connect_ip="158.160.54.160",
        source_ip="192.168.0.18",
    )

    assert client.complete("system", "user") == "ok"
    assert any(type(handler).__name__ == "_SourceAddressHTTPSHandler" for handler in handlers)
    assert opened[0][1] == 120


def test_yandex_client_uses_system_route_when_source_address_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yandex_module, "usable_source_ipv4", lambda _value: None)
    client = YandexAIClient(
        "key",
        "folder",
        connect_ip="158.160.54.160",
        source_ip="192.168.0.18",
    )

    assert client._opener is None


def test_yandex_client_rejects_invalid_source_address() -> None:
    with pytest.raises(ValueError):
        YandexAIClient("key", "folder", source_ip="not-an-ip")
    with pytest.raises(ValueError):
        YandexAIClient(
            "key",
            "folder",
            connect_ip="not-an-ip",
            source_ip="192.168.0.18",
        )
    with pytest.raises(ValueError, match="исходящий"):
        YandexAIClient(
            "key",
            "folder",
            connect_ip="158.160.54.160",
        )


def test_yandex_client_journals_call_and_reported_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Готово"}}]},
        {
            "choices": [{"delta": {"content": ""}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
            },
        },
    ]
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            [*(f"data: {json.dumps(chunk)}\n".encode() for chunk in chunks), b"data: [DONE]\n"]
        ),
    )
    journal = OperationJournal(tmp_path)
    client = YandexAIClient(
        "key",
        "folder",
        journal=journal,
        operation="connection_check",
    )

    assert client.complete("system", "user") == "Готово"

    entries = list(journal.entries())
    assert {entry["run_id"] for entry in entries} == {entries[0]["run_id"]}
    assert sum(entry.get("details", {}).get("model_calls", 0) for entry in entries) == 1
    completed = next(entry for entry in entries if entry["status"] == "completed")
    assert completed["component"] == "yandex_ai"
    assert completed["event"] == "model.complete"
    assert completed["details"]["prompt_units"] == 12
    assert completed["details"]["completion_units"] == 3
    assert completed["details"]["total_units"] == 15
    assert completed["details"]["usage_reported"] is True
    assert completed["details"]["usage_unit"] == "tokens"


def test_yandex_client_rejects_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse([b"data: [DONE]\n"]),
    )

    with pytest.raises(YandexAIError, match="пустой ответ"):
        YandexAIClient("key", "folder").complete("system", "user")


def test_yandex_client_limits_total_stream_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(
        [
            b'data: {"choices":[{"delta":{"content":"part"}}]}\n',
            b"data: [DONE]\n",
        ]
    )
    timeouts: list[float] = []
    response.fp = SimpleNamespace(
        raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append))
    )
    moments = iter((10.0, 10.0, 12.0))
    monkeypatch.setattr("hugin.adapters.yandex_ai.monotonic", lambda: next(moments))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(YandexAIError, match="Истекло время"):
        YandexAIClient("key", "folder", timeout_seconds=1).complete("system", "user")

    assert timeouts == [1.0]


@pytest.mark.parametrize(
    ("api_key", "folder_id", "message"),
    [("", "folder", "ключ"), ("key", "", "каталога")],
)
def test_yandex_client_requires_configuration(
    api_key: str,
    folder_id: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        YandexAIClient(api_key, folder_id)


@pytest.mark.parametrize("timeout", [0, 301])
def test_yandex_client_rejects_invalid_timeout(timeout: int) -> None:
    with pytest.raises(ValueError, match="ожидания"):
        YandexAIClient("key", "folder", timeout_seconds=timeout)


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_yandex_client_rejects_invalid_temperature(temperature: float) -> None:
    with pytest.raises(ValueError, match="Температура"):
        YandexAIClient("key", "folder", temperature=temperature)


def test_yandex_client_preserves_full_model_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> FakeResponse:
        captured.append(request)
        return FakeResponse([b'data: {"choices":[{"message":{"content":"ok"}}]}\n'])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = YandexAIClient("key", "folder", "gpt://other/model")

    assert client.complete("system", "user") == "ok"
    assert isinstance(captured[0].data, bytes)
    assert json.loads(captured[0].data.decode())["model"] == "gpt://other/model"


def test_yandex_client_uses_deep_reasoning_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> FakeResponse:
        captured.append(request)
        return FakeResponse([b'data: {"choices":[{"message":{"content":"ok"}}]}\n'])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = YandexAIClient("key", "folder", "qwen3-235b-a22b-fp8/latest")

    assert client.complete("system", "user") == "ok"
    assert isinstance(captured[0].data, bytes)
    body = json.loads(captured[0].data.decode())
    assert body["reasoning_effort"] == "high"


def test_yandex_client_uses_selected_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, **_kwargs: object) -> FakeResponse:
        captured.append(request)
        return FakeResponse([b'data: {"choices":[{"message":{"content":"ok"}}]}\n'])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = YandexAIClient("key", "folder", reasoning_effort="medium")

    assert client.complete("system", "user") == "ok"
    assert isinstance(captured[0].data, bytes)
    assert json.loads(captured[0].data.decode())["reasoning_effort"] == "medium"


def test_yandex_client_rejects_unknown_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="Режим обработки"):
        YandexAIClient("key", "folder", reasoning_effort="unknown")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (urllib.error.URLError("offline"), "недоступен"),
        (TimeoutError(), "Истекло время"),
        (OSError("broken"), "Ошибка запроса"),
    ],
)
def test_yandex_client_reports_network_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    def urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(YandexAIError, match=message):
        YandexAIClient("key", "folder").complete("system", "user")


def test_yandex_client_reports_http_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error = urllib.error.HTTPError(
        "https://example.test",
        401,
        "denied",
        Message(),
        BytesIO(b"access denied"),
    )

    def urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(YandexAIError, match="HTTP 401"):
        YandexAIClient(
            "key",
            "folder",
            journal=OperationJournal(tmp_path),
            operation="connection_check",
        ).complete("system", "user")

    failed = next(
        entry for entry in OperationJournal(tmp_path).entries() if entry["status"] == "failed"
    )
    assert failed["event"] == "model.complete"
    assert failed["details"]["error_type"] == "YandexAIError"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not-json", ""),
        ("{}", ""),
        ('{"choices":[null]}', ""),
        ('{"choices":[{"delta":null,"message":{"content":"text"}}]}', "text"),
    ],
)
def test_yandex_chunk_parser_handles_supported_shapes(payload: str, expected: str) -> None:
    assert YandexAIClient._parse_chunk(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not-json", {}),
        ("{}", {}),
        ('{"usage":[]}', {}),
        (
            '{"usage":{"input_tokens":4,"output_tokens":2,"totalTokens":6}}',
            {"prompt_units": 4, "completion_units": 2, "total_units": 6},
        ),
        (
            '{"usage":{"inputTextTokens":3,"completionTokens":1,'
            '"total_tokens":4,"prompt_tokens":true,"output_tokens":-1}}',
            {"total_units": 4},
        ),
    ],
)
def test_yandex_usage_parser_accepts_only_non_negative_integer_counters(
    payload: str,
    expected: dict[str, int],
) -> None:
    assert YandexAIClient._parse_usage(payload) == expected
