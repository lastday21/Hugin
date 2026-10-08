from __future__ import annotations

import threading
from pathlib import Path
from queue import Queue

import pytest
from playwright.sync_api import sync_playwright

from hugin.adapters.hh_browser import VisibleHhBrowser, _BrowserProfileLock
from hugin.core.settings import Settings


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
