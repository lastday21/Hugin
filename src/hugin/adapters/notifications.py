from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass

from hugin.adapters.notification_credentials import NotificationGatewayCredentials
from hugin.adapters.notification_gateway import NotificationGatewayClient


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


class NotificationGatewaySender:
    def __init__(
        self,
        base_url: str,
        credentials: NotificationGatewayCredentials,
        timeout_seconds: int = 15,
    ) -> None:
        self._base_url = base_url
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds

    def send(
        self,
        *,
        event_id: str,
        channel: str,
        event_type: str,
        content: NotificationContent,
        action_url: str | None = None,
    ) -> None:
        NotificationGatewayClient(
            self._base_url,
            self._credentials,
            timeout_seconds=self._timeout_seconds,
        ).send(
            event_id,
            channel,
            event_type,
            content.title,
            content.body,
            action_url,
        )
