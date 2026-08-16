from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from hugin.domain.vacancies import VacancyAvailability

_CLOSED_MARKERS = (
    "вакансия в архиве",
    "вакансия закрыта",
    "вакансия недоступна",
    "вакансия не найдена",
)


class HhVacancyStatusProbe:
    def __init__(
        self,
        transport: Callable[..., Any] | None = None,
        *,
        timeout_seconds: float = 4.0,
    ) -> None:
        self._transport = transport or urlopen
        self._timeout_seconds = timeout_seconds

    def check(self, source_url: str) -> VacancyAvailability | None:
        target = self._safe_vacancy_url(source_url)
        if target is None:
            return None
        request = Request(
            target,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Hugin/0.1",
            },
        )
        try:
            response = self._transport(request, timeout=self._timeout_seconds)
            with response:
                status = int(getattr(response, "status", 200))
                content = response.read(2_000_000)
        except HTTPError as error:
            if error.code in {404, 410}:
                return VacancyAvailability.UNAVAILABLE
            return None
        except (TimeoutError, URLError, OSError):
            return None
        if status in {404, 410}:
            return VacancyAvailability.UNAVAILABLE
        if status != 200 or not isinstance(content, bytes):
            return None
        page_text = content.decode("utf-8", errors="ignore").casefold()
        if any(marker in page_text for marker in _CLOSED_MARKERS):
            return VacancyAvailability.ARCHIVED
        return VacancyAvailability.ACTIVE

    @staticmethod
    def _safe_vacancy_url(source_url: str) -> str | None:
        try:
            parsed = urlparse(source_url.strip())
        except ValueError:
            return None
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or (hostname != "hh.ru" and not hostname.endswith(".hh.ru"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return None
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "vacancy" or not parts[1].isdigit():
            return None
        return urlunparse(("https", parsed.netloc, f"/vacancy/{parts[1]}", "", "", ""))
