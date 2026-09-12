from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from casetrace.contracts import Failure, FailureCode, RunEvent
from casetrace.evidence import EvidenceWriter

CANARIES = (
    "member-canary-91827",
    "account-canary-4815162342",
    "token-canary-deadbeef",
    "typed-canary-supersecret",
)


def _event(summary: str, *, seq: int, kind: str = "action") -> RunEvent:
    return RunEvent(
        event_id=f"event-{seq}",
        seq=seq,
        run_id="run-evidence",
        timestamp=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        kind=kind,
        actor="automation",
        summary=summary,
        step_id="step-1",
        observation_id=None,
        evidence_refs=[],
    )


def test_persisted_events_are_allowlisted_redacted_and_sequenced(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path, "run-evidence", sensitive_values=CANARIES)
    writer.emit(_event(f"filled field with {CANARIES[3]}", seq=0))
    writer.emit(
        _event(
            "provider error body=" + CANARIES[2] + " traceback at private/module.py:9",
            seq=1,
            kind="recovery",
        )
    )
    writer.emit(
        _event(
            "opened https://bank.invalid/activity?member_id="
            + CANARIES[0]
            + "&account="
            + CANARIES[1],
            seq=2,
        )
    )

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file() and path.suffix != ".png"
    )
    assert all(canary not in text for canary in CANARIES)
    assert "?member_id=" not in text
    rows = [json.loads(line) for line in writer.events_path.read_text().splitlines()]
    assert [row["seq"] for row in rows] == [0, 1, 2]
    assert all("__dict__" not in row and "payload" not in row for row in rows)


def test_writer_rejects_out_of_order_or_wrong_run_events(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path, "run-evidence")
    writer.emit(_event("safe", seq=0))

    with pytest.raises(ValueError, match="sequence"):
        writer.emit(_event("safe", seq=0))
    wrong = _event("safe", seq=1).model_copy(update={"run_id": "different-run"})
    with pytest.raises(ValueError, match="run"):
        writer.emit(wrong)


def test_finish_persists_redacted_result_projection(tmp_path) -> None:
    writer = EvidenceWriter(tmp_path, "run-evidence", sensitive_values=CANARIES)
    result = Failure(
        run_id="run-evidence",
        artifact_digest=None,
        binding_digest=None,
        evidence_refs=[],
        code=FailureCode.UNKNOWN_STATE,
        current_step="step-1",
        expected_condition="known page",
        observed_condition="raw provider body " + CANARIES[2],
        attempt_count=1,
    )

    writer.finish(result)

    persisted = writer.result_path.read_text(encoding="utf-8")
    assert CANARIES[2] not in persisted
    assert "[REDACTED]" in persisted
