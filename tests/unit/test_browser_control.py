import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hugin.browser_control import BrowserControl


def test_control_expires_and_stale_owner_cannot_release_new_control(tmp_path: Path) -> None:
    control = BrowserControl(tmp_path, 1)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    first = control.acquire("https://hh.ru/account/captcha", minutes=5, now=now)
    with pytest.raises(RuntimeError, match="передан"):
        control.acquire(None, now=now)
    assert control.active(now=now)
    assert not control.active(now=now + timedelta(minutes=6))
    second = control.acquire(None, now=now + timedelta(minutes=6))
    assert first["token"] != second["token"]
    with pytest.raises(RuntimeError, match="другому"):
        control.release(str(first["token"]))
    assert control.read()["token"] == second["token"]
    control.release(str(second["token"]))
    assert not control.active(now=now + timedelta(minutes=6))


def test_control_validates_destination_and_readiness_owner(tmp_path: Path) -> None:
    control = BrowserControl(tmp_path, 1)
    with pytest.raises(ValueError):
        control.acquire("https://example.com/account/captcha")
    request = control.acquire("https://uchaly.hh.ru/account/captcha")
    assert not control.mark_ready("old-owner")
    assert control.mark_ready(str(request["token"]))
    assert control.read()["state"] == "ready"
    control.release(str(request["token"]))


def test_background_handover_does_not_request_a_visible_window(tmp_path: Path) -> None:
    control = BrowserControl(tmp_path, 1)
    request = control.acquire(None, show_window=False)
    assert control.read()["show_window"] is False
    assert control.mark_ready(str(request["token"]))
    assert control.read()["show_window"] is False
    control.release(str(request["token"]))


def test_expired_request_cannot_be_marked_ready(tmp_path: Path) -> None:
    control = BrowserControl(tmp_path, 1)
    now = datetime.now(UTC)
    request = control.acquire(None, minutes=1, now=now)
    assert not control.mark_ready(str(request["token"]), now=now + timedelta(minutes=2))
    assert control.read()["state"] == "requested"


def test_cli_timeout_releases_only_its_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hugin import browser_control_cli
    from hugin.core.settings import Settings

    monkeypatch.setattr(browser_control_cli, "get_settings", lambda: Settings(data_dir=tmp_path))
    with pytest.raises(RuntimeError, match="не подтвердил"):
        browser_control_cli.main(["acquire", "--timeout", "0"])
    assert not BrowserControl(tmp_path, 1).active()


def test_saved_control_cannot_change_destination_outside_hh(tmp_path: Path) -> None:
    control = BrowserControl(tmp_path, 1)
    request = control.acquire("https://hh.ru/account/captcha")
    request["url"] = "https://example.com/"
    control.path.write_text(json.dumps(request), "utf8")
    with pytest.raises(ValueError):
        control.active()
