import json

import pytest

from hugin.services.recruiter_reply_review import review_recruiter_reply


class Model:
    model_name = "review-test"

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.requests.append((system_prompt, user_prompt))
        return self.response


def response(**changes: object) -> str:
    data = {"supported": True, "complete": True, "questions": [], "reason": "Supported facts"}
    data.update(changes)
    return json.dumps(data)


def test_positive_claim_and_question_are_reviewed_against_same_facts() -> None:
    model = Model(response(supported=False, reason="Invented commercial experience"))
    review = review_recruiter_reply(model, "Original question and confirmed facts", "Five years")
    assert not review.supported
    assert "Original question and confirmed facts" in model.requests[0][1]
    assert "Five years" in model.requests[0][1]


def test_missing_information_has_concrete_candidate_questions() -> None:
    review = review_recruiter_reply(
        Model(response(complete=False, questions=["Hours per week?"])), "context", "reply"
    )
    assert review.supported and not review.complete
    assert review.questions == ("Hours per week?",)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "{}",
        response(supported="true"),
        response(complete=1),
        response(questions="unknown"),
        response(questions=[False]),
        response(complete=False, questions=[""]),
        response(complete=False),
        response(questions=["Question?"]),
        response(reason=""),
        response(reason=None),
    ],
)
def test_invalid_review_never_approves_reply(raw: str) -> None:
    with pytest.raises(ValueError):
        review_recruiter_reply(Model(raw), "context", "reply")


def test_structured_output_is_used_when_available() -> None:
    class Structured(Model):
        def complete_json(
            self, system_prompt: str, user_prompt: str, schema: dict[str, object]
        ) -> str:
            assert schema["additionalProperties"] is False
            return response()

    model = Structured("must not use plain complete")
    assert review_recruiter_reply(model, "context", "reply").complete
    assert model.requests == []
