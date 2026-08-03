from __future__ import annotations

import json
from collections.abc import Iterator
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from hugin.adapters.notification_credentials import (
    NotificationGatewayCredentials,
    WindowsNotificationCredentialStore,
    load_notification_gateway_credentials,
)
from hugin.adapters.notification_gateway import (
    GatewayTransport,
    NotificationGatewayAuthorizationError,
    NotificationGatewayClient,
    NotificationGatewayDeliveryError,
    NotificationGatewayError,
    NotificationGatewayNotReadyError,
    NotificationGatewayTimeout,
)

SERVICE_KEY = "s" * 32


class Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class Transport:
    def __init__(self, *responses: Response | Exception) -> None:
        self._responses: Iterator[Response | Exception] = iter(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: int) -> Response:
        assert timeout == 15
        self.requests.append(request)
        selected = next(self._responses)
        if isinstance(selected, Exception):
            raise selected
        return selected


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


def http_error(code: int, payload: object) -> HTTPError:
    return HTTPError(
        "http://127.0.0.1:8088/ready",
        code,
        "failed",
        Message(),
        BytesIO(json.dumps(payload).encode()),
    )


def client(transport: GatewayTransport) -> NotificationGatewayClient:
    return NotificationGatewayClient(
        "http://127.0.0.1:8088",
        NotificationGatewayCredentials(SERVICE_KEY),
        transport=transport,
    )


def test_gateway_status_accepts_ready_and_structured_not_ready_response() -> None:
    ready_transport = Transport(
        Response({"status": "ok"}),
        Response({"status": "ok", "telegram": True, "paired": True, "email": True}),
    )
    status = client(ready_transport).status()

    assert status.available
    assert status.telegram
    assert status.paired
    assert status.email
    assert all(request.get_header("X-hugin-key") is None for request in ready_transport.requests)

    not_ready_transport = Transport(
        Response({"status": "ok"}),
        http_error(
            503,
            {
                "detail": {
                    "status": "not_ready",
                    "storage": True,
                    "telegram": True,
                    "paired": False,
                    "email": True,
                }
            },
        ),
    )

    not_ready = client(not_ready_transport).status()

    assert not_ready.available
    assert not_ready.telegram
    assert not_ready.paired is False
    assert not_ready.email


def test_gateway_pairs_waits_sends_and_tests_email_without_exposing_key() -> None:
    pairing_url = "https://t.me/hugin_workbot?start=one_time_code"
    transport = Transport(
        Response({"url": pairing_url, "expires_at": "2026-08-03T12:00:00+00:00"}),
        Response({"status": "ok"}),
        http_error(
            503,
            {
                "detail": {
                    "status": "not_ready",
                    "storage": True,
                    "telegram": True,
                    "paired": False,
                    "email": True,
                }
            },
        ),
        Response({"status": "ok"}),
        Response({"status": "ok", "telegram": True, "paired": True, "email": True}),
        Response(
            {
                "notification_id": 41,
                "channel": "TELEGRAM",
                "state": "SENT",
                "duplicate": False,
            }
        ),
        Response({"status": "sent", "recipient": "owner@example.test"}),
    )
    gateway = NotificationGatewayClient(
        "http://127.0.0.1:8088",
        NotificationGatewayCredentials(SERVICE_KEY),
        transport=transport,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    pairing = gateway.create_pairing_link()
    status = gateway.wait_until_paired(timeout_seconds=30)
    gateway.send(
        "message:123:NEW_MESSAGE:TELEGRAM",
        "telegram",
        "new_message",
        "Новое сообщение",
        "Отклик: Python-разработчик",
        "https://hh.ru/vacancy/123",
    )
    gateway.send_test_email()

    assert pairing.start_url == pairing_url
    assert status.paired
    assert "one_time_code" not in repr(pairing)
    protected = [request for request in transport.requests if request.data is not None]
    assert all(request.get_header("X-hugin-key") == SERVICE_KEY for request in protected)
    assert protected[-2].get_header("Content-type") == "application/json; charset=utf-8"
    assert "Новое сообщение".encode() in cast(bytes, protected[-2].data)
    sent_payload = json.loads(cast(bytes, transport.requests[-2].data))
    assert sent_payload == {
        "event_id": "message:123:NEW_MESSAGE:TELEGRAM",
        "channel": "TELEGRAM",
        "event_type": "NEW_MESSAGE",
        "title": "Новое сообщение",
        "body": "Отклик: Python-разработчик",
        "action_url": "https://hh.ru/vacancy/123",
    }


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        (401, NotificationGatewayAuthorizationError),
        (409, NotificationGatewayNotReadyError),
        (502, NotificationGatewayDeliveryError),
    ],
)
def test_gateway_maps_safe_http_errors(
    code: int,
    error_type: type[Exception],
) -> None:
    gateway = client(Transport(http_error(code, {"detail": "private response"})))

    with pytest.raises(error_type) as captured:
        gateway.create_pairing_link()

    assert "private response" not in str(captured.value)
    assert SERVICE_KEY not in str(captured.value)


def test_gateway_rejects_unsafe_urls_inputs_and_invalid_responses() -> None:
    credentials = NotificationGatewayCredentials(SERVICE_KEY)
    with pytest.raises(ValueError, match="адрес"):
        NotificationGatewayClient("http://notifications.example", credentials)
    with pytest.raises(ValueError, match="ожидания"):
        NotificationGatewayClient("http://127.0.0.1:8088", credentials, timeout_seconds=0)

    unsafe_pairing = client(
        Transport(
            Response(
                {
                    "url": "https://example.org/hugin_workbot?start=code",
                    "expires_at": "2026-08-03T12:00:00+00:00",
                }
            )
        )
    )
    with pytest.raises(NotificationGatewayError, match="небезопасную"):
        unsafe_pairing.create_pairing_link()

    invalid_delivery = client(
        Transport(
            Response(
                {
                    "notification_id": 1,
                    "channel": "TELEGRAM",
                    "state": "PENDING",
                    "duplicate": False,
                }
            )
        )
    )
    with pytest.raises(NotificationGatewayDeliveryError, match="не подтвердила"):
        invalid_delivery.send(
            "event-1",
            "TELEGRAM",
            "NEW_MESSAGE",
            "Заголовок",
            "Текст",
        )

    with pytest.raises(ValueError, match=r"hh\.ru"):
        invalid_delivery.send(
            "event-2",
            "TELEGRAM",
            "NEW_MESSAGE",
            "Заголовок",
            "Текст",
            "https://example.org/vacancy/1",
        )


@pytest.mark.parametrize(
    "responses",
    [
        (Response({"status": "wrong"}),),
        (Response({"status": "ok"}), Response({"status": "wrong"})),
        (
            Response({"status": "ok"}),
            http_error(503, {"detail": {"status": "wrong", "storage": True}}),
        ),
        (
            Response({"status": "ok"}),
            http_error(
                503,
                {
                    "detail": {
                        "status": "not_ready",
                        "storage": "yes",
                        "telegram": True,
                        "paired": False,
                        "email": True,
                    }
                },
            ),
        ),
        (
            Response({"status": "ok"}),
            Response({"status": "ok", "telegram": "yes", "paired": False, "email": True}),
        ),
    ],
)
def test_gateway_rejects_invalid_status_payloads(
    responses: tuple[Response | Exception, ...],
) -> None:
    with pytest.raises(NotificationGatewayError, match="некорректное состояние"):
        client(Transport(*responses)).status()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": "https://t.me/hugin_workbot?start=code", "expires_at": "not-a-date"},
        {"url": "https://t.me/hugin_workbot?start=code", "expires_at": "2026-08-03T12:00:00"},
    ],
)
def test_gateway_rejects_invalid_pairing_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(NotificationGatewayError):
        client(Transport(Response(payload))).create_pairing_link()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("", "TELEGRAM", "NEW_MESSAGE", "Заголовок", "Текст"), "идентификатор"),
        (("event-1", "WINDOWS", "NEW_MESSAGE", "Заголовок", "Текст"), "канал"),
        (("event-1", "TELEGRAM", "UNKNOWN", "Заголовок", "Текст"), "вид"),
        (("event-1", "TELEGRAM", "NEW_MESSAGE", "", "Текст"), "заголовок"),
        (("event-1", "TELEGRAM", "NEW_MESSAGE", "Заголовок", ""), "текст"),
    ],
)
def test_gateway_validates_notification_fields(
    values: tuple[str, str, str, str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        client(Transport()).send(*values)


def test_gateway_rejects_unavailable_pairing_and_bad_email_confirmation() -> None:
    credentials = NotificationGatewayCredentials(SERVICE_KEY)
    with pytest.raises(ValueError, match="адрес"):
        NotificationGatewayClient("http://127.0.0.1:bad", credentials)
    with pytest.raises(ValueError, match="подключения"):
        client(Transport()).wait_until_paired(timeout_seconds=0)

    unavailable = NotificationGatewayClient(
        "http://127.0.0.1:8088",
        credentials,
        transport=Transport(
            Response({"status": "ok"}),
            Response({"status": "ok", "telegram": False, "paired": False, "email": True}),
        ),
        clock=lambda: 0.0,
    )
    with pytest.raises(NotificationGatewayNotReadyError, match="не готова"):
        unavailable.wait_until_paired(timeout_seconds=30)

    with pytest.raises(NotificationGatewayDeliveryError, match="проверочное письмо"):
        client(Transport(Response({"status": "failed", "recipient": ""}))).send_test_email()


def test_gateway_reports_timeout_network_error_and_malformed_json() -> None:
    moments = iter((0.0, 2.0))
    timeout = NotificationGatewayClient(
        "http://127.0.0.1:8088",
        NotificationGatewayCredentials(SERVICE_KEY),
        clock=lambda: next(moments),
    )
    with pytest.raises(NotificationGatewayTimeout, match="истекло"):
        timeout.wait_until_paired(timeout_seconds=1)

    def unavailable(_request: Request, *, timeout: int) -> Response:
        assert timeout == 15
        raise URLError("offline")

    with pytest.raises(NotificationGatewayError, match="недоступна"):
        client(cast(GatewayTransport, unavailable)).status()

    class RawResponse(Response):
        def __init__(self, body: bytes) -> None:
            self._body = body

    malformed = client(Transport(RawResponse(b"{")))
    with pytest.raises(NotificationGatewayError, match="некорректный ответ"):
        malformed.status()


def test_notification_gateway_credentials_prefer_file_and_hide_secret(tmp_path: Path) -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)
    stored = NotificationGatewayCredentials("w" * 32)
    store.save_notification_gateway(stored)
    key_file = tmp_path / "service_key"
    key_file.write_text("f" * 32, encoding="utf-8")

    loaded = load_notification_gateway_credentials(key_file, store)

    assert loaded == NotificationGatewayCredentials("f" * 32)
    assert "f" * 32 not in repr(loaded)
    assert backend.values[("Hugin.notifications", "notification.service_key")] == "w" * 32
    assert load_notification_gateway_credentials(None, store) == stored
    assert store.delete_notification_gateway()
    assert load_notification_gateway_credentials(None, store) is None


def test_notification_gateway_credentials_reject_bad_file_and_store(
    tmp_path: Path,
) -> None:
    backend = FakeKeyring()
    store = WindowsNotificationCredentialStore(backend)
    missing = tmp_path / "missing"
    with pytest.raises(RuntimeError, match="недоступен"):
        load_notification_gateway_credentials(missing, store)

    invalid = tmp_path / "service_key"
    invalid.write_text("short-secret", encoding="utf-8")
    with pytest.raises(RuntimeError, match="повреждён") as captured:
        load_notification_gateway_credentials(invalid, store)
    assert "short-secret" not in str(captured.value)

    backend.values[("Hugin.notifications", "notification.service_key")] = "bad"
    with pytest.raises(RuntimeError, match="повреждён"):
        store.load_notification_gateway()
