from __future__ import annotations

import subprocess
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from hugin.adapters.notification_credentials import load_notification_gateway_credentials
from hugin.adapters.notification_gateway import (
    NotificationGatewayClient,
    NotificationGatewayConnectionError,
    NotificationGatewayError,
)
from hugin.core.settings import Settings
from hugin.diagnostics import OperationJournal

_RECOVERY_COOLDOWN_SECONDS = 300.0


class NotificationGatewayRecovery:
    """Поднимает только уже настроенную соседнюю местную службу уведомлений."""

    def __init__(
        self,
        settings: Settings,
        *,
        project_directory: Path | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self._settings = settings
        self._project_directory = project_directory or Path(__file__).resolve().parents[2]
        self._journal = journal or OperationJournal(settings.data_dir)
        self._last_attempt_at: float | None = None

    def ensure_running(self) -> bool:
        now = monotonic()
        if (
            self._last_attempt_at is not None
            and now - self._last_attempt_at < _RECOVERY_COOLDOWN_SECONDS
        ):
            return False
        self._last_attempt_at = now
        try:
            return self._ensure_running()
        except Exception as error:
            self._journal.record(
                "notifications",
                "gateway.recovery",
                status="failed",
                level="WARNING",
                error_type=type(error).__name__,
            )
            return False

    def _ensure_running(self) -> bool:
        target = urlsplit(self._settings.notification_gateway_url)
        if (
            target.scheme != "http"
            or target.hostname not in {"127.0.0.1", "localhost"}
            or target.port != 8088
            or target.username is not None
            or target.password is not None
            or target.path not in {"", "/"}
            or target.query
            or target.fragment
        ):
            return False
        gateway = self._project_directory.parent / "Hugin_tg_bot"
        compose = gateway / "compose.yaml"
        if not compose.is_file():
            return False
        credentials = load_notification_gateway_credentials(
            self._settings.notification_gateway_key_file
        )
        if credentials is None:
            return False
        client = NotificationGatewayClient(
            self._settings.notification_gateway_url, credentials, timeout_seconds=2
        )
        try:
            return client.status().available
        except NotificationGatewayConnectionError:
            pass
        except NotificationGatewayError:
            return False
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--file",
                str(compose),
                "--project-directory",
                str(gateway),
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "20",
                "hugin-tg-bot",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        ready = result.returncode == 0 and client.status().available
        self._journal.record(
            "notifications",
            "gateway.recovery",
            status="completed" if ready else "failed",
            level="INFO" if ready else "WARNING",
            returncode=result.returncode,
            available=ready,
        )
        return ready


def ensure_notification_gateway_running(
    settings: Settings,
    *,
    project_directory: Path | None = None,
    journal: OperationJournal | None = None,
) -> bool:
    return NotificationGatewayRecovery(
        settings, project_directory=project_directory, journal=journal
    ).ensure_running()
