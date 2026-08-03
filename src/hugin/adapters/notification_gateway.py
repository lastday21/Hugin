from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, sleep
from typing import Never, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from hugin.adapters.notification_credentials import NotificationGatewayCredentials

_EVENT_ID = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
_CHANNELS = frozenset({"TELEGRAM", "EMAIL"})
_EVENT_TYPES = frozenset(
    {
        "NEW_MESSAGE",
        "INVITATION",
        "REPLY_REQUIRED",
        "FORM_REQUIRED",
        "AUTH_REQUIRED",
        "ACCOUNT_WARNING",
        "UNKNOWN_RESULT",
        "CRITICAL_ERROR",
        "DAILY_SUMMARY",
    }
)


class NotificationGatewayError(RuntimeError):
    pass


class NotificationGatewayAuthorizationError(NotificationGatewayError):
    pass


class NotificationGatewayNotReadyError(NotificationGatewayError):
    pass


class NotificationGatewayTimeout(NotificationGatewayNotReadyError):
    pass


class NotificationGatewayDeliveryError(NotificationGatewayError):
    pass


@dataclass(frozen=True, slots=True)
class NotificationGatewayStatus:
    available: bool
    telegram: bool | None
    paired: bool | None
    email: bool | None


@dataclass(frozen=True, slots=True)
class PairingLink:
    start_url: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "PairingLink("
            f"start_url={self.start_url.split('?', 1)[0]!r}, "
            f"expires_at={self.expires_at!r})"
        )


class HttpResponse(Protocol):
    status: int

    def __enter__(self) -> HttpResponse: ...

    def __exit__(self, *_args: object) -> None: ...

    def read(self) -> bytes: ...


class GatewayTransport(Protocol):
    def __call__(self, request: Request, *, timeout: int) -> HttpResponse: ...


class NotificationGatewayClient:
    def __init__(
        self,
        base_url: str,
        credentials: NotificationGatewayCredentials,
        *,
        timeout_seconds: int = 15,
        transport: GatewayTransport | None = None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        selected = base_url.strip().rstrip("/")
        try:
            target = urlsplit(selected)
            port = target.port
        except ValueError:
            target = urlsplit("")
            port = None
        local_http = target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost"}
        if (
            not selected
            or target.scheme not in {"http", "https"}
            or (target.scheme != "https" and not local_http)
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.path not in {"", "/"}
            or target.query
            or target.fragment
            or (port is None and ":" in target.netloc.rsplit("]", 1)[-1])
        ):
            raise ValueError("Некорректный адрес службы уведомлений")
        if timeout_seconds < 1 or timeout_seconds > 60:
            raise ValueError("Некорректное время ожидания службы уведомлений")
        self._base_url = f"{selected}/"
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        if transport is None and local_http:
            transport = cast(GatewayTransport, build_opener(ProxyHandler({})).open)
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper

    def status(self) -> NotificationGatewayStatus:
        live = self._request("GET", "live")
        if live.get("status") != "ok":
            raise NotificationGatewayError("Служба уведомлений вернула некорректное состояние")

        ready = self._request("GET", "ready", allow_not_ready=True)
        detail = ready.get("detail")
        if detail is not None:
            if not isinstance(detail, dict) or detail.get("status") != "not_ready":
                raise NotificationGatewayError("Служба уведомлений вернула некорректное состояние")
            storage = detail.get("storage")
            if not isinstance(storage, bool):
                raise NotificationGatewayError("Служба уведомлений вернула некорректное состояние")
            return NotificationGatewayStatus(
                storage,
                self._optional_bool(detail, "telegram"),
                self._optional_bool(detail, "paired"),
                self._optional_bool(detail, "email"),
            )

        if ready.get("status") != "ok":
            raise NotificationGatewayError("Служба уведомлений вернула некорректное состояние")
        return NotificationGatewayStatus(
            True,
            self._optional_bool(ready, "telegram"),
            self._optional_bool(ready, "paired"),
            self._optional_bool(ready, "email"),
        )

    def create_pairing_link(self) -> PairingLink:
        payload = self._request(
            "POST",
            "api/v1/pairing-links",
            body={},
            authorized=True,
        )
        start_url = payload.get("url")
        expires_at = payload.get("expires_at")
        if not isinstance(start_url, str) or not isinstance(expires_at, str):
            raise NotificationGatewayError("Служба уведомлений вернула неполные данные подключения")
        selected_url = self._validate_pairing_url(start_url)
        try:
            selected_expiration = datetime.fromisoformat(expires_at)
        except ValueError:
            raise NotificationGatewayError(
                "Служба уведомлений вернула некорректный срок подключения"
            ) from None
        if selected_expiration.tzinfo is None or selected_expiration.utcoffset() is None:
            raise NotificationGatewayError(
                "Служба уведомлений вернула некорректный срок подключения"
            )
        return PairingLink(selected_url, selected_expiration)

    def wait_until_paired(self, *, timeout_seconds: int) -> NotificationGatewayStatus:
        if timeout_seconds < 1 or timeout_seconds > 900:
            raise ValueError("Некорректное время подключения Telegram")
        deadline = self._clock() + timeout_seconds
        while self._clock() < deadline:
            status = self.status()
            if not status.available or status.telegram is False:
                raise NotificationGatewayNotReadyError(
                    "Служба Telegram ещё не готова к подключению"
                )
            if status.paired is True:
                return status
            self._sleeper(1.0)
        raise NotificationGatewayTimeout(
            "Время подключения истекло. Нажмите «Подключить Telegram» ещё раз."
        )

    def send(
        self,
        event_id: str,
        channel: str,
        event_type: str,
        title: str,
        body: str,
        action_url: str | None = None,
    ) -> None:
        selected_event_id = event_id.strip()
        selected_channel = channel.strip().upper()
        selected_event_type = event_type.strip().upper()
        selected_title = title.strip()
        selected_body = body.strip()
        if not _EVENT_ID.fullmatch(selected_event_id):
            raise ValueError("Некорректный идентификатор уведомления")
        if selected_channel not in _CHANNELS:
            raise ValueError("Некорректный канал уведомления")
        if selected_event_type not in _EVENT_TYPES:
            raise ValueError("Некорректный вид уведомления")
        if not selected_title or len(selected_title) > 200:
            raise ValueError("Некорректный заголовок уведомления")
        if not selected_body or len(selected_body) > 1_000:
            raise ValueError("Некорректный текст уведомления")

        payload: dict[str, object] = {
            "event_id": selected_event_id,
            "channel": selected_channel,
            "event_type": selected_event_type,
            "title": selected_title,
            "body": selected_body,
        }
        if action_url is not None:
            payload["action_url"] = self._validate_hh_url(action_url)
        response = self._request(
            "POST",
            "api/v1/notifications",
            body=payload,
            authorized=True,
        )
        notification_id = response.get("notification_id")
        duplicate = response.get("duplicate")
        if (
            not isinstance(notification_id, int)
            or isinstance(notification_id, bool)
            or notification_id < 1
            or response.get("channel") != selected_channel
            or response.get("state") != "SENT"
            or not isinstance(duplicate, bool)
        ):
            raise NotificationGatewayDeliveryError("Служба уведомлений не подтвердила доставку")

    def send_test_email(self) -> None:
        payload = self._request(
            "POST",
            "api/v1/email/test",
            body={},
            authorized=True,
        )
        recipient = payload.get("recipient")
        if (
            payload.get("status") != "sent"
            or not isinstance(recipient, str)
            or not recipient.strip()
        ):
            raise NotificationGatewayDeliveryError(
                "Служба уведомлений не подтвердила проверочное письмо"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        authorized: bool = False,
        allow_not_ready: bool = False,
    ) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        if authorized:
            headers["X-Hugin-Key"] = self._credentials.service_key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            urljoin(self._base_url, path),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            transport: Callable[..., HttpResponse] = self._transport or urlopen
            with transport(request, timeout=self._timeout_seconds) as response:
                response_body = response.read()
        except HTTPError as error:
            if allow_not_ready and error.code == 503:
                return self._error_payload(error)
            self._raise_http_error(error.code)
        except (URLError, TimeoutError, OSError):
            raise NotificationGatewayError("Служба уведомлений сейчас недоступна") from None
        return self._json_object(response_body)

    @classmethod
    def _error_payload(cls, error: HTTPError) -> dict[str, object]:
        try:
            body = error.read()
        except OSError:
            raise NotificationGatewayError(
                "Служба уведомлений вернула некорректное состояние"
            ) from None
        return cls._json_object(body)

    @staticmethod
    def _raise_http_error(code: int) -> Never:
        if code == 401:
            raise NotificationGatewayAuthorizationError("Служба уведомлений отклонила ключ связи")
        if code in {409, 503}:
            raise NotificationGatewayNotReadyError("Служба уведомлений ещё не готова")
        if code in {422, 502}:
            raise NotificationGatewayDeliveryError("Служба уведомлений не приняла сообщение")
        raise NotificationGatewayError("Служба уведомлений не выполнила запрос")

    @staticmethod
    def _json_object(body: bytes) -> dict[str, object]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NotificationGatewayError(
                "Служба уведомлений вернула некорректный ответ"
            ) from None
        if not isinstance(payload, dict):
            raise NotificationGatewayError("Служба уведомлений вернула некорректный ответ")
        return payload

    @staticmethod
    def _optional_bool(payload: dict[str, object], key: str) -> bool | None:
        value = payload.get(key)
        if value is not None and not isinstance(value, bool):
            raise NotificationGatewayError("Служба уведомлений вернула некорректное состояние")
        return value

    @staticmethod
    def _validate_pairing_url(value: str) -> str:
        selected = value.strip()
        try:
            target = urlsplit(selected)
            port = target.port
            query = parse_qs(target.query, strict_parsing=True)
        except ValueError:
            raise NotificationGatewayError(
                "Служба уведомлений вернула небезопасную ссылку"
            ) from None
        if (
            target.scheme != "https"
            or target.hostname != "t.me"
            or target.username is not None
            or target.password is not None
            or port not in {None, 443}
            or not re.fullmatch(r"/[A-Za-z0-9_]{5,32}", target.path)
            or set(query) != {"start"}
            or len(query["start"]) != 1
            or not query["start"][0]
            or target.fragment
        ):
            raise NotificationGatewayError("Служба уведомлений вернула небезопасную ссылку")
        return selected

    @staticmethod
    def _validate_hh_url(value: str) -> str:
        selected = value.strip()
        try:
            target = urlsplit(selected)
            port = target.port
        except ValueError:
            raise ValueError("Разрешены только безопасные ссылки на hh.ru") from None
        hostname = target.hostname
        if (
            target.scheme != "https"
            or hostname is None
            or not (hostname == "hh.ru" or hostname.endswith(".hh.ru"))
            or target.username is not None
            or target.password is not None
            or port not in {None, 443}
            or target.fragment
        ):
            raise ValueError("Разрешены только безопасные ссылки на hh.ru")
        return selected
