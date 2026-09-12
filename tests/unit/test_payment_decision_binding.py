import pytest
from pydantic import ValidationError

from casetrace.contracts import Capability, ReturnStep
from tests.unit.test_contracts import _artifact


def decision_step():
    step = _artifact()["steps"][-1]
    return step | {
        "result_kind": "payment_decision",
        "result": {
            "kind": "payment_decision",
            "record_steps": ["rows"],
            "detail_steps": ["detail"],
        },
    }


def test_explicit_payment_decision_terminal():
    terminal = ReturnStep.model_validate(decision_step())
    assert terminal.result.record_steps == ["rows"]


def test_decision_cannot_reference_nonexistent_reads():
    artifact = _artifact()
    artifact["steps"][-1] = decision_step()
    with pytest.raises(ValidationError, match="payment decision.*read"):
        Capability.model_validate(artifact)


@pytest.mark.parametrize("field", ["record_steps", "detail_steps"])
def test_decision_requires_unique_nonempty_read_ids(field):
    step = decision_step()
    step["result"][field] = []
    with pytest.raises(ValidationError):
        ReturnStep.model_validate(step)
    step["result"][field] = ["same", "same"]
    with pytest.raises(ValidationError):
        ReturnStep.model_validate(step)
