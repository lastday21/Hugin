from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from types import TracebackType
from typing import Self

import pytest

from hugin.adapters.yandex_ai import YandexAIClient, YandexAIError


class FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

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


def test_yandex_client_rejects_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse([b"data: [DONE]\n"]),
    )

    with pytest.raises(YandexAIError, match="пустой ответ"):
        YandexAIClient("key", "folder").complete("system", "user")


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


def test_yandex_client_reports_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
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
        YandexAIClient("key", "folder").complete("system", "user")


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
