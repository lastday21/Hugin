import pytest

from hugin.workers.model_turn import ModelTurn


class Model:
    model_name = "test"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return "готово"


def test_turn_checks_stop_before_each_request() -> None:
    allowed = True
    source = Model()
    guarded = ModelTurn(lambda: allowed).wrap(source)
    assert guarded.model_name == "test"
    assert guarded.complete("", "") == "готово"
    allowed = False
    with pytest.raises(RuntimeError, match="остановлен"):
        guarded.complete("", "")
    assert source.calls == 1


def test_expired_turn_does_not_start_another_request() -> None:
    source = Model()
    guarded = ModelTurn(lambda: True, seconds=-1).wrap(source)
    with pytest.raises(RuntimeError, match="истекло"):
        guarded.complete("", "")
    assert source.calls == 0
