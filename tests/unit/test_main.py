import pytest

from hugin import __main__ as cli
from hugin.core.settings import Settings


def test_main_starts_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        environment="test",
        api_host="127.0.0.1",
        api_port=8123,
        log_level="WARNING",
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def run(app: str, **kwargs: object) -> None:
        calls.append((app, kwargs))

    def settings_factory() -> Settings:
        return settings

    monkeypatch.setattr(cli, "get_settings", settings_factory)
    monkeypatch.setattr("hugin.__main__.uvicorn.run", run)

    cli.main()

    assert calls == [
        (
            "hugin.api.app:create_app",
            {
                "factory": True,
                "host": "127.0.0.1",
                "port": 8123,
                "log_level": "warning",
            },
        )
    ]
