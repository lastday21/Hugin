from __future__ import annotations

import threading
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from queue import Queue
from time import monotonic, sleep
from typing import Literal, cast

import pytest
from playwright.sync_api import sync_playwright

from hugin.adapters.hh_browser import VisibleHhBrowser, _BrowserProfileLock
from hugin.core.settings import Settings


@pytest.mark.parametrize("cancel_during", ["snapshot", "navigation"])
def test_cancelled_control_does_not_break_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel_during: str
) -> None:
    from hugin.adapters import shared_hh_browser as shared

    automatic = threading.Lock()
    manager = shared.SharedHhBrowser(Settings(data_dir=tmp_path), browser_locks=(automatic,))
    request = manager.control.acquire("https://hh.ru/account/captcha")
    original = manager.control.read_active
    first_read = True

    def read_active(*, now: datetime | None = None) -> dict[str, object]:
        nonlocal first_read
        value = original(now=now)
        if first_read and cancel_during == "snapshot":
            first_read = False
            manager.control.release(str(request["token"]))
        return value

    class Browser:
        def ensure_visible(self) -> None:
            pass

        def debugging_endpoint(self) -> str:
            return "http://127.0.0.1:12345"

        def show_verification(self, _url: str) -> None:
            manager.control.release(str(request["token"]))

    monkeypatch.setattr(manager.control, "read_active", read_active)
    with ExitStack() as held:
        assert (
            manager._manual_tick(
                cast(VisibleHhBrowser, Browser()), shared._BrowserOwner(), held, None
            )
            is None
        )
        assert automatic.acquire(blocking=False)
        automatic.release()


def test_failed_manual_entry_releases_guard_without_releasing_next_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hugin.adapters import shared_hh_browser as shared

    manager = shared.SharedHhBrowser(Settings(data_dir=tmp_path))
    monkeypatch.setattr(manager, "endpoint", lambda: "http://127.0.0.1:12345")
    original = manager.control.read_active
    replaced = False
    new_token = ""

    def replace_owner(*, now: datetime | None = None) -> dict[str, object]:
        nonlocal replaced, new_token
        value = original(now=now)
        if value and not replaced:
            replaced = True
            manager.control.release(str(value["token"]))
            value = manager.control.acquire(None)
            new_token = str(value["token"])
        return value

    monkeypatch.setattr(manager.control, "read_active", replace_owner)
    lock = manager.manual_lock()
    with pytest.raises(RuntimeError, match="отменена"), lock:
        pass
    assert manager.control.read()["token"] == new_token
    assert lock._guard.acquire(blocking=False)
    lock._guard.release()


def test_manual_handover_waits_for_workers_and_restores_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hugin.adapters import shared_hh_browser as shared

    shown = threading.Event()
    opened: list[dict[str, object]] = []
    modes: list[str] = []

    class Browser:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            opened.append(kwargs)

        def __enter__(self) -> Browser:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def debugging_endpoint(self) -> str:
            return "http://127.0.0.1:12345"

        def is_open(self) -> bool:
            return True

        def show_verification(self, url: str) -> None:
            assert url == "https://hh.ru/account/captcha"
            shown.set()

        def ensure_visible(self) -> None:
            modes.append("visible")

        def return_to_background(self) -> None:
            modes.append("background")

    monkeypatch.setattr(shared, "VisibleHhBrowser", Browser)
    automatic = threading.Lock()
    manager = shared.SharedHhBrowser(
        Settings(environment="test", data_dir=tmp_path, hh_browser_channel="chrome"),
        browser_locks=(automatic,),
    )
    automatic.acquire()
    manager.start()
    try:
        request = manager.control.acquire("https://hh.ru/account/captcha")
        assert not manager.automatic_allowed()
        manager.endpoint()
        assert manager.control.read()["state"] == "requested"
        assert not shown.is_set()
        automatic.release()
        assert shown.wait(3)
        deadline = monotonic() + 3
        while manager.control.read()["state"] != "ready" and monotonic() < deadline:
            sleep(0.01)
        assert manager.control.read()["state"] == "ready"
        assert not automatic.acquire(blocking=False)
        assert opened[0]["browser_channel"] == "chrome"
        assert opened[0]["headless"] is True
        assert modes == ["visible"]
        manager.control.release(str(request["token"]))
        assert manager.automatic_allowed()
        assert automatic.acquire(timeout=3)
        automatic.release()
        assert "background" in modes
        with manager.manual_lock():
            assert not manager.automatic_allowed()
            assert not automatic.acquire(blocking=False)
        assert manager.automatic_allowed()
    finally:
        manager.stop()
        assert automatic.acquire(timeout=3)
        automatic.release()


@pytest.mark.parametrize("fail_first", [False, True])
def test_shared_owner_starts_once_and_closes_on_its_own_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_first: bool
) -> None:
    from hugin.adapters import shared_hh_browser as shared

    opened: list[int] = []
    closed: list[int] = []

    class Browser:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Browser:
            opened.append(threading.get_ident())
            if fail_first and len(opened) == 1:
                raise RuntimeError("Ошибка запуска браузера")
            return self

        def __exit__(self, *_args: object) -> None:
            closed.append(threading.get_ident())

        def debugging_endpoint(self) -> str:
            return "http://127.0.0.1:12345"

        def is_open(self) -> bool:
            return True

    monkeypatch.setattr(shared, "VisibleHhBrowser", Browser)
    manager = shared.SharedHhBrowser(Settings(environment="test", data_dir=tmp_path))
    endpoints: list[str] = []
    if fail_first:
        with pytest.raises(RuntimeError, match="Ошибка запуска браузера"):
            manager.endpoint()

    def connect() -> None:
        endpoints.append(manager.endpoint())

    threads = [threading.Thread(target=connect) for _ in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert endpoints == ["http://127.0.0.1:12345"] * 3
        assert len(opened) == (2 if fail_first else 1)
    finally:
        manager.stop()
    assert closed == [opened[-1]]
    with pytest.raises(RuntimeError, match="остановлен"):
        manager.endpoint()


def test_worker_threads_keep_independent_tabs_and_share_login_state(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = _BrowserProfileLock(profile / ".hugin-browser.lock", timeout_seconds=0)
    lock.acquire()
    errors: Queue[Exception] = Queue()
    both_open = threading.Barrier(2)
    first_closed = threading.Event()
    results: dict[str, str | None] = {}
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                headless=True,
                args=["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"],
            )
            try:
                context.add_cookies(
                    [{"name": "local-login", "value": "same-session", "url": "http://127.0.0.1"}]
                )
                port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
                endpoint = f"http://127.0.0.1:{port}"

                def work(name: str) -> None:
                    try:
                        with VisibleHhBrowser(
                            profile,
                            "login",
                            "resumes",
                            "search",
                            5000,
                            start_minimized=True,
                            shared_endpoint=lambda: endpoint,
                        ) as browser:
                            page = browser._require_page()
                            page.set_content(f"<p>{name}</p>")
                            both_open.wait(5)
                            if name == "second":
                                assert first_closed.wait(5)
                            results[name] = page.text_content("p")
                            assert browser._context is not None
                            assert browser._context.cookies()[0]["value"] == "same-session"
                    except Exception as error:
                        errors.put(error)
                    finally:
                        if name == "first":
                            first_closed.set()

                threads = [
                    threading.Thread(target=work, args=(name,)) for name in ("first", "second")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(15)
                    assert not thread.is_alive()
                if not errors.empty():
                    raise errors.get()
                assert results == {"first": "first", "second": "second"}
                assert context.cookies()[0]["value"] == "same-session"
                assert len(context.pages) == 1
            finally:
                context.close()
    finally:
        lock.release()


@pytest.mark.parametrize("channel", ["chromium", "msedge"])
def test_automatic_shared_browser_really_runs_without_a_window(
    tmp_path: Path, channel: Literal["chromium", "msedge"]
) -> None:
    from hugin.adapters.shared_hh_browser import SharedHhBrowser

    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        hh_browser_channel=channel,
        hh_browser_source_ip=None,
    )
    manager = SharedHhBrowser(settings)
    try:
        endpoint = manager.endpoint()
        with VisibleHhBrowser(
            settings.browser_profile_dir(1),
            "login",
            "resumes",
            "search",
            5000,
            start_minimized=True,
            shared_endpoint=lambda: endpoint,
        ) as worker:
            assert worker._context is not None and worker._context.browser is not None
            session = worker._context.browser.new_browser_cdp_session()
            try:
                assert "HeadlessChrome" in session.send("Browser.getVersion")["userAgent"]
            finally:
                session.detach()
            page = worker._require_page()
            page.set_content('<input aria-label="answer"><button>Next</button>')
            page.get_by_label("answer").fill("confirmed")
            page.get_by_role("button").click()
            assert page.get_by_label("answer").input_value() == "confirmed"
        assert manager.endpoint() == endpoint
    finally:
        manager.stop()
