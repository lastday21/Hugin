from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from hugin.core.settings import Settings
from hugin.database import create_database, postgresql_url
from hugin.domain.tasks import SystemState
from hugin.repositories.tasks import SystemStateRepository
from hugin.services.captcha_agent import CaptchaAgentDispatcher
from hugin.workers.captcha_agent import CaptchaAgentWorker

THREAD_ID = "00000000-0000-4000-8000-000000000001"
MESSAGE_ID = "00000000-0000-4000-8000-000000000002"


def queue_receipt(thread_id: str = THREAD_ID, message_id: str = MESSAGE_ID) -> str:
    return f"Queued message {message_id} for thread {thread_id}.\n"


@pytest.fixture
def desktop_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr("hugin.services.captcha_agent.os.startfile", urls.append, raising=False)
    return urls


def configured(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "captcha_agent_thread_id": THREAD_ID,
            "captcha_agent_codex_path": Path(sys.executable),
        }
    )


def protect(settings: Settings, *, paused: bool = False) -> None:
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            repository = SystemStateRepository(session)
            if not paused:
                if repository.get().state is SystemState.CAPTCHA_REQUIRED:
                    repository.resume_after_authentication()
                else:
                    repository.transition(SystemState.RUNNING)
            repository.transition(SystemState.CAPTCHA_REQUIRED)
    finally:
        database.close()


def test_request_is_durable_and_new_episode_can_wake_again(
    settings: Settings, desktop_urls: list[str]
) -> None:
    settings = configured(settings)
    commands: list[list[str]] = []

    def queue(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, queue_receipt(), "")

    dispatcher = CaptchaAgentDispatcher(settings, agent_request=queue)
    assert dispatcher.dispatch_current() is False
    protect(settings)
    assert dispatcher.dispatch_current() is True
    assert CaptchaAgentDispatcher(settings, agent_request=queue).dispatch_current() is False
    assert len(commands) == 1
    database = create_database(settings)
    try:
        with database.sessions.begin() as session:
            SystemStateRepository(session).set_next_apply_at(
                datetime.now(UTC) + timedelta(minutes=1)
            )
    finally:
        database.close()
    assert CaptchaAgentDispatcher(settings, agent_request=queue).dispatch_current() is False
    assert len(commands) == 1
    assert commands[0][1:6] == ["queue", "--thread", THREAD_ID, "--model", "gpt-6.1-sol"]
    assert commands[0][6:9] == ["-c", 'model_reasoning_effort="high"', "--message"]
    assert desktop_urls == [f"codex://threads/{THREAD_ID}"]
    assert "HUGIN_CAPTCHA_EVENT" in commands[0][-1]
    assert "UNKNOWN_RESULT" in commands[0][-1]
    assert "действующее разрешение пользователя" in commands[0][-1].replace("\n", " ")
    assert "если разрешение не подтверждено, остановись" in commands[0][-1]
    assert "текущего проекта" in commands[0][-1]
    record = json.loads(
        next((settings.data_dir / "captcha-agent").glob("*.json")).read_text("utf8")
    )
    assert record["status"] == "desktop_requested" and record["thread_id"] == THREAD_ID
    assert record["message_id"] == MESSAGE_ID
    assert Path(str(record["output_path"])).read_text("utf8") == queue_receipt()
    protect(settings)
    assert dispatcher.dispatch_current() is True
    assert len(commands) == 2
    assert desktop_urls == [f"codex://threads/{THREAD_ID}"] * 2


def test_queue_receipt_without_desktop_does_not_confirm_request(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = configured(settings)
    protect(settings)
    calls = 0

    def queue(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command, 0, f"Queued message {MESSAGE_ID} for thread {THREAD_ID}.\n", ""
        )

    def fail_open(_url: str) -> None:
        raise OSError("No registered desktop app")

    monkeypatch.setattr("hugin.services.captcha_agent.os.startfile", fail_open, raising=False)
    dispatcher = CaptchaAgentDispatcher(settings, agent_request=queue)
    assert dispatcher.dispatch_current() is False
    assert dispatcher.dispatch_current() is False
    assert calls == 1
    record = json.loads(
        next((settings.data_dir / "captcha-agent").glob("*.json")).read_text("utf8")
    )
    assert record["status"] == "desktop_failed"
    assert record["message_id"] == MESSAGE_ID


@pytest.mark.parametrize(
    "failure",
    (
        "oserror",
        "timeout",
        "unconfirmed",
        "missing-receipt",
        "other-thread",
        "invalid-message-id",
        "completed-without-queue",
    ),
)
def test_unconfirmed_request_is_not_repeated(
    settings: Settings, failure: str, desktop_urls: list[str]
) -> None:
    settings = configured(settings)
    protect(settings)
    calls = 0

    def queue(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if failure == "oserror":
            raise OSError("not started")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=b"Queued message ")
        if failure == "missing-receipt":
            return subprocess.CompletedProcess(command, 0, "accepted?", "")
        if failure == "other-thread":
            return subprocess.CompletedProcess(command, 0, queue_receipt(MESSAGE_ID), "")
        if failure == "invalid-message-id":
            return subprocess.CompletedProcess(
                command, 0, queue_receipt(message_id="not-a-uuid"), ""
            )
        if failure == "completed-without-queue":
            return subprocess.CompletedProcess(command, 0, '{"type":"turn.completed"}', "")
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    assert CaptchaAgentDispatcher(settings, agent_request=queue).dispatch_current() is False
    assert CaptchaAgentDispatcher(settings, agent_request=queue).dispatch_current() is False
    assert calls == 1
    record = json.loads(
        next((settings.data_dir / "captcha-agent").glob("*.json")).read_text("utf8")
    )
    assert record["status"] == {"oserror": "failed", "timeout": "unknown"}.get(
        failure, "unconfirmed"
    )
    if failure == "timeout":
        assert Path(record["output_path"]).read_text("utf8") == "Queued message "
    assert desktop_urls == []


def test_paused_state_and_disabled_agent_never_dispatch(settings: Settings) -> None:
    def queue(_command: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Paused or disabled must not wake an agent")

    protect(settings, paused=True)
    assert (
        CaptchaAgentDispatcher(configured(settings), agent_request=queue).dispatch_current()
        is False
    )
    assert CaptchaAgentDispatcher(settings, agent_request=queue).dispatch_current() is False
    worker = CaptchaAgentWorker(settings, dispatch=lambda: False)
    worker.start()
    worker.stop()
    assert not worker.running


def test_listener_waits_for_events_without_polling_and_stops(settings: Settings) -> None:
    settings = configured(settings)
    calls: list[float] = []
    notified = threading.Event()

    def dispatch() -> bool:
        calls.append(time.monotonic())
        if len(calls) >= 2:
            notified.set()
        return False

    worker = CaptchaAgentWorker(settings, dispatch=dispatch)
    worker.start()
    worker.start()
    try:
        assert worker.ready.wait(3)
        assert calls and worker.running
        time.sleep(0.15)
        assert len(calls) == 1
        protect(settings)
        assert notified.wait(3)
        assert len(calls) == 2
    finally:
        worker.stop()
    assert not worker.running
    assert len(calls) == 2


def test_listener_reconnects_after_failure(settings: Settings) -> None:
    settings = configured(settings)
    recovered = threading.Event()
    calls = 0

    def dispatch() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one broken connection handler")
        recovered.set()
        return False

    worker = CaptchaAgentWorker(settings, dispatch=dispatch)
    worker.start()
    try:
        assert worker.ready.wait(3)
        protect(settings)
        assert recovered.wait(5)
        assert calls == 2
    finally:
        worker.stop()
    assert not worker.running


def test_thread_id_accepts_blank_and_rejects_non_uuid() -> None:
    assert Settings(captcha_agent_thread_id=" ").captcha_agent_thread_id == ""
    assert Settings(captcha_agent_thread_id=THREAD_ID).captcha_agent_thread_id == THREAD_ID
    with pytest.raises(ValueError, match="captcha_agent_thread_id"):
        Settings(captcha_agent_thread_id="another chat")


def test_captcha_notification_follows_committed_working_transition(settings: Settings) -> None:
    database = create_database(settings)
    dsn = (
        postgresql_url(settings).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    try:
        with psycopg.connect(dsn, autocommit=True) as listener:
            listener.execute("LISTEN hugin_captcha_required")
            with database.sessions.begin() as session:
                SystemStateRepository(session).transition(SystemState.RUNNING)
            with (
                pytest.raises(RuntimeError, match="rollback"),
                database.sessions.begin() as session,
            ):
                SystemStateRepository(session).transition(SystemState.CAPTCHA_REQUIRED)
                raise RuntimeError("rollback")
            assert list(listener.notifies(timeout=0.05)) == []
            with database.sessions.begin() as session:
                SystemStateRepository(session).transition(SystemState.CAPTCHA_REQUIRED)
                assert list(listener.notifies(timeout=0.05)) == []
            notices = list(listener.notifies(timeout=0.1))
            assert len(notices) == 1
            assert json.loads(notices[0].payload)["state"] == "CAPTCHA_REQUIRED"
            with database.sessions.begin() as session:
                SystemStateRepository(session).resume_after_authentication()
                SystemStateRepository(session).transition(SystemState.PAUSED)
                SystemStateRepository(session).transition(SystemState.CAPTCHA_REQUIRED)
            assert list(listener.notifies(timeout=0.05)) == []
    finally:
        database.close()
