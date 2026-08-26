from __future__ import annotations

from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from hugin.adapters.hh_vacancy_status import HhVacancyStatusProbe
from hugin.domain.vacancies import VacancyAvailability


class FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self._body = BytesIO(body.encode("utf-8"))

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self, size: int) -> bytes:
        return self._body.read(size)


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("<main>Python-разработчик</main>", VacancyAvailability.ACTIVE),
        ("<h2>Вакансия в архиве</h2>", VacancyAvailability.ARCHIVED),
    ),
)
def test_vacancy_status_probe_reads_hh_page(
    body: str,
    expected: VacancyAvailability,
) -> None:
    requests: list[Request] = []

    def transport(request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == 4.0
        requests.append(request)
        return FakeResponse(body)

    result = HhVacancyStatusProbe(transport).check(
        "https://ufa.hh.ru/vacancy/135300718?from=applicant"
    )

    assert result is expected
    assert requests[0].full_url == "https://ufa.hh.ru/vacancy/135300718"
    assert requests[0].get_header("User-agent")


def test_vacancy_status_probe_handles_missing_and_unsafe_pages() -> None:
    def missing(request: Request, *, timeout: float) -> FakeResponse:
        raise HTTPError(request.full_url, 404, "not found", Message(), None)

    probe = HhVacancyStatusProbe(missing)

    assert probe.check("https://hh.ru/vacancy/135300718") is VacancyAvailability.UNAVAILABLE
    assert probe.check("https://example.com/vacancy/135300718") is None
    assert probe.check("https://hh.ru@example.com/vacancy/135300718") is None
