from __future__ import annotations

import base64
import json
import smtplib
import ssl
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hugin.adapters.notification_credentials import EmailCredentials, TelegramCredentials


@dataclass(frozen=True, slots=True)
class NotificationContent:
    title: str
    body: str


class WindowsToastSender:
    def send(self, content: NotificationContent) -> None:
        title = base64.b64encode(content.title.encode()).decode("ascii")
        body = base64.b64encode(content.body.encode()).decode("ascii")
        script = (
            "$ErrorActionPreference='Stop';"
            "[Windows.UI.Notifications.ToastNotificationManager,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
            "[Windows.UI.Notifications.ToastNotification,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
            "[Windows.Data.Xml.Dom.XmlDocument,"
            "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null;"
            f"$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title}'));"
            f"$b=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{body}'));"
            "$te=[Security.SecurityElement]::Escape($t);"
            "$be=[Security.SecurityElement]::Escape($b);"
            "$xml=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'>"
            '<text>$te</text><text>$be</text></binding></visual></toast>");'
            "$toast=New-Object Windows.UI.Notifications.ToastNotification $xml;"
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            "CreateToastNotifier('Hugin').Show($toast);"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            check=False,
            capture_output=True,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError("Windows не приняла уведомление")


class TelegramNotificationSender:
    def __init__(self, credentials: TelegramCredentials, timeout_seconds: int = 15) -> None:
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds

    def send(self, content: NotificationContent) -> None:
        payload = json.dumps(
            {
                "chat_id": self._credentials.chat_id,
                "text": f"{content.title}\n\n{content.body}",
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode()
        request = Request(
            f"https://api.telegram.org/bot{self._credentials.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            raise RuntimeError("Telegram не принял уведомление") from error
        try:
            result = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError("Telegram вернул некорректный ответ") from error
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram не подтвердил доставку уведомления")


class EmailNotificationSender:
    def __init__(self, credentials: EmailCredentials, timeout_seconds: int = 20) -> None:
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds

    def send(self, content: NotificationContent) -> None:
        message = EmailMessage()
        message["Subject"] = content.title
        message["From"] = self._credentials.sender
        message["To"] = self._credentials.recipient
        message.set_content(content.body)
        try:
            with smtplib.SMTP(
                self._credentials.smtp_host,
                self._credentials.smtp_port,
                timeout=self._timeout_seconds,
            ) as client:
                if self._credentials.starttls:
                    client.starttls(context=ssl.create_default_context())
                if self._credentials.username:
                    client.login(
                        self._credentials.username,
                        self._credentials.password,
                    )
                refused = client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise RuntimeError("Почтовый сервер не принял уведомление") from error
        if refused:
            raise RuntimeError("Получатель отклонил уведомление")
