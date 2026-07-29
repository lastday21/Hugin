from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

_ACCESS_TOKEN = re.compile(r"^hgt_[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9_-]{20,100}$")


class TelegramGatewayError(RuntimeError):
    pass


class TelegramGatewayAuthorizationError(TelegramGatewayError):
    pass


class TelegramGatewayTimeout(TelegramGatewayError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayStatus:
    bot_ready: bool
    bot_username: str


@dataclass(frozen=True, slots=True)
class Pairing:
    pairing_id: str
    pairing_secret: str
    start_url: str
    bot_username: str

    def __repr__(self) -> str:
        return (
            "Pairing("
            f"pairing_id={self.pairing_id!r}, pairing_secret='***', "
            f"start_url={self.start_url.split('?', 1)[0]!r}, "
            f"bot_username={self.bot_username!r})"
        )


@dataclass(frozen=True, slots=True)
class GatewayConnection:
    access_token: str
    bot_username: str

    def __repr__(self) -> str:
        return f"GatewayConnection(access_token='***', bot_username={self.bot_username!r})"


class HttpResponse(Protocol):
    status: int

    def __enter__(self) -> HttpResponse: ...

    def __exit__(self, *_args: object) -> None: ...

    def read(self) -> bytes: ...


class GatewayTransport(Protocol):
    def __call__(self, request: Request, *, timeout: int) -> HttpResponse: ...


class TelegramGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 15,
        transport: GatewayTransport | None = None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        selected = base_url.strip().rstrip("/")
        target = urlsplit(selected)
        local_http = target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost"}
        if (
            not selected
            or target.scheme not in {"http", "https"}
            or (target.scheme != "https" and not local_http)
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
        ):
            raise ValueError("Некорректный адрес службы Telegram")
        if timeout_seconds < 1 or timeout_seconds > 60:
            raise ValueError("Некорректное время ожидания службы Telegram")
        self._base_url = f"{selected}/"
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper

    def status(self) -> GatewayStatus:
        payload = self._request("GET", "v1/status")
        bot_ready = payload.get("bot_ready")
        bot_username = payload.get("bot_username")
        if not isinstance(bot_ready, bool) or not isinstance(bot_username, str):
            raise TelegramGatewayError("Служба Telegram вернула некорректное состояние")
        return GatewayStatus(bot_ready, self._username(bot_username))

    def create_pairing(self) -> Pairing:
        payload = self._request("POST", "v1/pairings", body={})
        pairing_id = payload.get("pairing_id")
        pairing_secret = payload.get("pairing_secret")
        start_url = payload.get("start_url")
        bot_username = payload.get("bot_username")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (pairing_id, pairing_secret, start_url, bot_username)
        ):
            raise TelegramGatewayError("Служба Telegram вернула неполные данные подключения")
        assert isinstance(pairing_id, str)
        assert isinstance(pairing_secret, str)
        assert isinstance(start_url, str)
        assert isinstance(bot_username, str)
        username = self._username(bot_username)
        self._validate_start_url(start_url, username)
        return Pairing(
            pairing_id.strip(),
            pairing_secret.strip(),
            start_url.strip(),
            username,
        )

    def wait_for_connection(
        self,
        pairing: Pairing,
        *,
        timeout_seconds: int,
    ) -> GatewayConnection:
        if timeout_seconds < 1 or timeout_seconds > 900:
            raise ValueError("Некорректное время подключения Telegram")
        deadline = self._clock() + timeout_seconds
        while self._clock() < deadline:
            payload = self._request(
                "GET",
                f"v1/pairings/{pairing.pairing_id}",
                authorization=f"Pairing {pairing.pairing_secret}",
            )
            state = payload.get("status")
            if state == "pending":
                self._sleeper(1.0)
                continue
            access_token = payload.get("access_token")
            bot_username = payload.get("bot_username")
            if (
                state != "connected"
                or not isinstance(access_token, str)
                or not _ACCESS_TOKEN.fullmatch(access_token)
                or not isinstance(bot_username, str)
            ):
                raise TelegramGatewayError("Служба Telegram вернула некорректное подключение")
            return GatewayConnection(access_token, self._username(bot_username))
        raise TelegramGatewayTimeout(
            "Время подключения истекло. Нажмите «Подключить Telegram» ещё раз."
        )

    def connection_status(self, access_token: str) -> GatewayStatus:
        selected = self._access_token(access_token)
        payload = self._request(
            "GET",
            "v1/connections/me",
            authorization=f"Bearer {selected}",
        )
        if payload.get("status") != "connected":
            raise TelegramGatewayError("Служба Telegram не подтвердила подключение")
        bot_username = payload.get("bot_username")
        if not isinstance(bot_username, str):
            raise TelegramGatewayError("Служба Telegram вернула неполное состояние")
        return GatewayStatus(True, self._username(bot_username))

    def disconnect(self, access_token: str) -> None:
        selected = self._access_token(access_token)
        self._request(
            "DELETE",
            "v1/connections/me",
            authorization=f"Bearer {selected}",
            expect_empty=True,
        )

    def send(self, access_token: str, title: str, body: str) -> None:
        selected = self._access_token(access_token)
        title_text = title.strip()
        body_text = body.strip()
        if not title_text or not body_text:
            raise ValueError("Уведомление Telegram не заполнено")
        self._request(
            "POST",
            "v1/notifications",
            authorization=f"Bearer {selected}",
            body={"title": title_text, "body": body_text},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        authorization: str | None = None,
        body: dict[str, object] | None = None,
        expect_empty: bool = False,
    ) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode()
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
            if error.code == 401:
                raise TelegramGatewayAuthorizationError(
                    "Подключение Telegram устарело или было отключено"
                ) from None
            if error.code == 410:
                raise TelegramGatewayTimeout(
                    "Время подключения истекло. Нажмите «Подключить Telegram» ещё раз."
                ) from None
            if error.code == 503:
                raise TelegramGatewayError("Служба Telegram ещё не готова") from None
            raise TelegramGatewayError("Служба Telegram не выполнила запрос") from None
        except (URLError, TimeoutError, OSError):
            raise TelegramGatewayError("Служба Telegram сейчас недоступна") from None
        if expect_empty:
            return {}
        try:
            payload = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramGatewayError("Служба Telegram вернула некорректный ответ") from None
        if not isinstance(payload, dict):
            raise TelegramGatewayError("Служба Telegram вернула некорректный ответ")
        return payload

    @staticmethod
    def _access_token(value: str) -> str:
        selected = value.strip()
        if not _ACCESS_TOKEN.fullmatch(selected):
            raise ValueError("Сохранённое подключение Telegram повреждено")
        return selected

    @staticmethod
    def _username(value: str) -> str:
        selected = value.strip().removeprefix("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", selected):
            raise TelegramGatewayError("Служба Telegram вернула некорректное имя бота")
        return selected

    @staticmethod
    def _validate_start_url(value: str, username: str) -> None:
        target = urlsplit(value.strip())
        if (
            target.scheme != "https"
            or target.hostname != "t.me"
            or target.username is not None
            or target.password is not None
            or target.path != f"/{username}"
            or not target.query.startswith("start=")
            or target.fragment
        ):
            raise TelegramGatewayError("Служба Telegram вернула небезопасную ссылку")
