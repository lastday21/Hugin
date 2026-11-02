from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from hugin.api.concurrent_reads import ConcurrentReads


@pytest.mark.parametrize("fail", [False, True])
def test_concurrent_reads_share_only_running_work_and_retry(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    entered, release, joined = Event(), Event(), Event()

    class ObservedFuture(Future[object]):
        def result(self, timeout: float | None = None) -> object:
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr("hugin.api.concurrent_reads.Future", ObservedFuture)
    reads = ConcurrentReads()
    calls = 0

    def load() -> int:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        if fail:
            raise ValueError("failed read")
        return 10

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reads.run, ("forms", 1), load)
        assert entered.wait(2)
        second = pool.submit(reads.run, ("forms", 1), load)
        try:
            assert joined.wait(2)
            assert reads.run(("forms", 2), lambda: 20) == 20
            assert reads.run(("processes", 1), lambda: 30) == 30
            assert calls == 1
        finally:
            release.set()
        for result in (first, second):
            if fail:
                with pytest.raises(ValueError, match="failed read"):
                    result.result(timeout=2)
            else:
                assert result.result(timeout=2) == 10
    assert reads.run(("forms", 1), lambda: 40) == 40


def test_write_invalidation_preserves_new_inflight_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_old, release_old = Event(), Event()
    entered_new, release_new, joined = Event(), Event(), Event()

    class ObservedFuture(Future[object]):
        def result(self, timeout: float | None = None) -> object:
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr("hugin.api.concurrent_reads.Future", ObservedFuture)
    reads = ConcurrentReads()

    def load(entered: Event, release: Event, value: int) -> int:
        entered.set()
        assert release.wait(5)
        return value

    with ThreadPoolExecutor(max_workers=3) as pool:
        old = pool.submit(reads.run, ("forms", 1), lambda: load(entered_old, release_old, 1))
        assert entered_old.wait(2)
        reads.invalidate()
        new = pool.submit(reads.run, ("forms", 1), lambda: load(entered_new, release_new, 2))
        try:
            assert entered_new.wait(2)
            release_old.set()
            assert old.result(timeout=2) == 1
            follower = pool.submit(reads.run, ("forms", 1), lambda: 3)
            assert joined.wait(2)
        finally:
            release_old.set()
            release_new.set()
        assert new.result(timeout=2) == follower.result(timeout=2) == 2
