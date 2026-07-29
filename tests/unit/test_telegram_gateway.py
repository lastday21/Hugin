from __future__ import annotations

import json
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from hugin.adapters.telegram_gateway import (
    GatewayTransport,
    Pairing,
    TelegramGatewayAuthorizationError,
    TelegramGatewayClient,
    TelegramGatewayError,
    TelegramGatewayTimeout,
)

ACCESS_TOKEN = "hgt_connectionid.abcdefghijklmnopqrstuvwxyzABCDEFG"


class Response:
    status = 200

    def __init__(self, payload: object | None = None) -> None:
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class Transport:
    def __init__(self, *responses: Response) -> None:
        self._responses: Iterator[Response] = iter(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: int) -> Response:
        assert timeout == 15
        self.requests.append(request)
        return next(self._responses)


def test_gateway_client_pairs_sends_and_disconnects_without_bot_token() -> None:
    transport = Transport(
        Response({"bot_ready": True, "bot_username": "hugin_workbot"}),
        Response(
            {
                "pairing_id": "pairing-id",
                "pairing_secret": "pairing-secret",
                "start_url": "https://t.me/hugin_workbot?start=one_time",
                "bot_username": "hugin_workbot",
            }
        ),
        Response({"status": "pending"}),
        Response(
            {
                "status": "connected",
                "access_token": ACCESS_TOKEN,
                "bot_username": "hugin_workbot",
            }
        ),
        Response({"status": "connected", "bot_username": "hugin_workbot"}),
        Response({"status": "accepted"}),
        Response(),
    )
    client = TelegramGatewayClient(
        "https://telegram.example",
        transport=transport,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    assert client.status().bot_ready
    pairing = client.create_pairing()
    connection = client.wait_for_connection(pairing, timeout_seconds=30)
    assert client.connection_status(connection.access_token).bot_ready
    client.send(connection.access_token, "Hugin", "Новое сообщение")
    client.disconnect(connection.access_token)

    assert "pairing-secret" not in repr(pairing)
    assert ACCESS_TOKEN not in repr(connection)
    assert all("api.telegram.org" not in request.full_url for request in transport.requests)
    assert transport.requests[-2].headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert transport.requests[-1].get_method() == "DELETE"


def test_gateway_client_allows_http_only_for_local_trial() -> None:
    TelegramGatewayClient("http://127.0.0.1:8020")
    TelegramGatewayClient("http://localhost:8020")

    with pytest.raises(ValueError, match="адрес"):
        TelegramGatewayClient("http://telegram.example")


def test_gateway_client_reports_revoked_connection_without_leaking_token() -> None:
    def unauthorized(_request: Request, *, timeout: int) -> Response:
        assert timeout == 15
        raise HTTPError(
            "https://telegram.example/v1/connections/me",
            401,
            "unauthorized",
            Message(),
            BytesIO(b"secret response"),
        )

    client = TelegramGatewayClient(
        "https://telegram.example",
        transport=cast(GatewayTransport, unauthorized),
    )

    with pytest.raises(TelegramGatewayAuthorizationError) as captured:
        client.connection_status(ACCESS_TOKEN)

    assert ACCESS_TOKEN not in str(captured.value)


def test_gateway_client_rejects_invalid_service_payloads_and_inputs() -> None:
    with pytest.raises(ValueError, match="ожидания"):
        TelegramGatewayClient("https://telegram.example", timeout_seconds=0)

    invalid_status = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(Response({"bot_ready": "yes"})),
    )
    with pytest.raises(TelegramGatewayError, match="состояние"):
        invalid_status.status()

    incomplete_pairing = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(Response({"pairing_id": "only-one-field"})),
    )
    with pytest.raises(TelegramGatewayError, match="неполные"):
        incomplete_pairing.create_pairing()

    unsafe_pairing = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(
            Response(
                {
                    "pairing_id": "id",
                    "pairing_secret": "secret",
                    "start_url": "https://example.org/hugin_workbot?start=code",
                    "bot_username": "hugin_workbot",
                }
            )
        ),
    )
    with pytest.raises(TelegramGatewayError, match="небезопасную"):
        unsafe_pairing.create_pairing()

    invalid_username = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(Response({"bot_ready": True, "bot_username": "bad name"})),
    )
    with pytest.raises(TelegramGatewayError, match="имя бота"):
        invalid_username.status()

    with pytest.raises(ValueError, match="повреждено"):
        invalid_status.connection_status("bad-token")
    with pytest.raises(ValueError, match="не заполнено"):
        invalid_status.send(ACCESS_TOKEN, "", "body")
    with pytest.raises(ValueError, match="время подключения"):
        invalid_status.wait_for_connection(
            Pairing("id", "secret", "https://t.me/hugin_workbot?start=code", "hugin_workbot"),
            timeout_seconds=0,
        )


def test_gateway_client_rejects_invalid_connection_and_times_out() -> None:
    pairing = Pairing(
        "id",
        "secret",
        "https://t.me/hugin_workbot?start=code",
        "hugin_workbot",
    )
    invalid = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(
            Response(
                {
                    "status": "connected",
                    "access_token": "invalid",
                    "bot_username": "hugin_workbot",
                }
            )
        ),
        clock=lambda: 0.0,
    )
    with pytest.raises(TelegramGatewayError, match="некорректное подключение"):
        invalid.wait_for_connection(pairing, timeout_seconds=30)

    moments = iter((0.0, 2.0))
    timeout = TelegramGatewayClient(
        "https://telegram.example",
        clock=lambda: next(moments),
    )
    with pytest.raises(TelegramGatewayTimeout, match="истекло"):
        timeout.wait_for_connection(pairing, timeout_seconds=1)

    wrong_state = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(
            Response({"status": "pending", "bot_username": "hugin_workbot"}),
            Response({"status": "connected"}),
        ),
    )
    with pytest.raises(TelegramGatewayError, match="не подтвердила"):
        wrong_state.connection_status(ACCESS_TOKEN)
    with pytest.raises(TelegramGatewayError, match="неполное состояние"):
        wrong_state.connection_status(ACCESS_TOKEN)


@pytest.mark.parametrize(
    ("code", "error_type", "message"),
    [
        (410, TelegramGatewayTimeout, "истекло"),
        (503, TelegramGatewayError, "не готова"),
        (500, TelegramGatewayError, "не выполнила"),
    ],
)
def test_gateway_client_maps_safe_http_errors(
    code: int,
    error_type: type[Exception],
    message: str,
) -> None:
    def failed(_request: Request, *, timeout: int) -> Response:
        assert timeout == 15
        raise HTTPError(
            "https://telegram.example/v1/status",
            code,
            "failed",
            Message(),
            BytesIO(b"secret response"),
        )

    client = TelegramGatewayClient(
        "https://telegram.example",
        transport=cast(GatewayTransport, failed),
    )

    with pytest.raises(error_type, match=message):
        client.status()


def test_gateway_client_rejects_network_and_malformed_json() -> None:
    def unavailable(_request: Request, *, timeout: int) -> Response:
        assert timeout == 15
        raise URLError("offline")

    client = TelegramGatewayClient(
        "https://telegram.example",
        transport=cast(GatewayTransport, unavailable),
    )
    with pytest.raises(TelegramGatewayError, match="недоступна"):
        client.status()

    class RawResponse(Response):
        def __init__(self, body: bytes) -> None:
            self._body = body

    malformed = TelegramGatewayClient(
        "https://telegram.example",
        transport=Transport(RawResponse(b"{"), RawResponse(b"[]")),
    )
    with pytest.raises(TelegramGatewayError, match="некорректный ответ"):
        malformed.status()
    with pytest.raises(TelegramGatewayError, match="некорректный ответ"):
        malformed.status()
