import pytest

from casetrace.contracts import PaymentQuery
from scripts.resume_candidate import (
    canonicalize_candidate,
    load_saved_recording,
    preserve_raw_candidate,
)


def test_load_saved_recording_reconstructs_compiler_evidence() -> None:
    raw = {
        "run_id": "saved-run",
        "goal": "Find the payment",
        "initial_surface": {
            "vendor": "Northstar Synthetic Bank",
            "app_version": "2026.09",
            "surface_features": ["web"],
        },
        "provider_calls": {"finish": {"model_id": "qwen3.5:9b", "call_index": 17}},
        "operations": [
            {
                "event_id": "event-1",
                "observation_id": "observation-0",
                "resulting_observation_id": "observation-1",
                "step": {
                    "step_id": "discovery-1",
                    "kind": "navigate",
                    "url": {"kind": "variable", "name": "entry_url", "field": None},
                    "checkpoint": None,
                    "checkpoint_role": None,
                    "checks": [],
                    "provenance": {
                        "kind": "observed",
                        "event_ids": ["event-1"],
                        "validation_scenario": None,
                    },
                },
                "target": None,
            }
        ],
    }
    query = PaymentQuery.model_validate(
        {
            "member_id": "12345",
            "amount": "250.00",
            "currency": "USD",
            "date_from": "2026-09-01",
            "date_to": "2026-09-07",
            "reference": None,
        }
    )

    recording = load_saved_recording(raw, query)

    assert recording.run_id == "saved-run"
    assert recording.event_ids == {"event-1"}
    assert recording.operations[0].step.kind == "navigate"
    assert recording.initial_observation.vendor == "Northstar Synthetic Bank"
    assert recording.sensitive_values == ("12345", "250.00", "2026-09-01", "2026-09-07")


def test_load_saved_recording_rejects_empty_operations() -> None:
    query = PaymentQuery.model_validate(
        {
            "member_id": "12345",
            "amount": "250.00",
            "currency": "USD",
            "date_from": "2026-09-01",
            "date_to": "2026-09-07",
            "reference": None,
        }
    )

    with pytest.raises(ValueError, match="no recorded operations"):
        load_saved_recording(
            {
                "run_id": "saved-run",
                "goal": "Find the payment",
                "initial_surface": {},
                "provider_calls": {},
                "operations": [],
            },
            query,
        )


def test_canonicalize_candidate_repairs_only_fixed_schema_fields() -> None:
    candidate = {
        "capability_id": "northstar-payment-query",
        "capability_version": "1.0",
        "steps": [
            {
                "kind": "assert",
                "target_id": "target-16-3",
                "predicate": {"kind": "visible", "target_id": "target-16-3"},
            }
        ],
        "vendor": "Northstar Synthetic Bank",
    }

    repaired = canonicalize_candidate(candidate)

    assert repaired["capability_id"] == "trace_incoming_payment"
    assert repaired["capability_version"] == "0.1.0"
    assert "target_id" not in repaired["steps"][0]
    assert repaired["steps"][0]["predicate"]["target_id"] == "target-16-3"
    assert repaired["vendor"] == "Northstar Synthetic Bank"
    assert candidate["capability_id"] == "northstar-payment-query"
    assert candidate["steps"][0]["target_id"] == "target-16-3"


def test_preserve_raw_candidate_keeps_existing_attempt(tmp_path) -> None:
    candidate = tmp_path / "candidate.raw.json"
    candidate.write_text('{"attempt": 2}\n', encoding="utf-8")
    (tmp_path / "candidate.raw.previous-1.json").write_text(
        '{"attempt": 1}\n', encoding="utf-8"
    )

    backup = preserve_raw_candidate(candidate)

    assert backup == tmp_path / "candidate.raw.previous-2.json"
    assert backup.read_text(encoding="utf-8") == '{"attempt": 2}\n'
    assert not candidate.exists()
