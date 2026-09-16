from __future__ import annotations

from copy import deepcopy

import pytest

from casetrace.compiler import CompilationError, compile_candidate
from casetrace.contracts import (
    AccessibleRoleStrategy,
    AssertStep,
    Capability,
    CapabilityScope,
    CheckpointRole,
    ExecutionLimits,
    InputContract,
    LiteralValue,
    NavigateStep,
    Observation,
    OutputContract,
    Provenance,
    ReturnStep,
    TargetSpec,
    TenantBindings,
    VariableValue,
    VisiblePredicate,
)
from casetrace.discovery import DiscoveryRecording, RecordedOperation


def _bindings() -> TenantBindings:
    return TenantBindings(
        tenant_id="northstar-synthetic",
        vendor="Northstar Synthetic Bank",
        origin="http://127.0.0.1:8000",
        app_version="2026.09",
        timezone="America/Chicago",
    )


def _candidate_and_recording() -> tuple[Capability, DiscoveryRecording]:
    target = TargetSpec(
        target_id="member-heading",
        surface_kind="web",
        frame_path=["Bank workspace"],
        strategies=[AccessibleRoleStrategy(role="heading", name="Member search")],
    )
    visible = VisiblePredicate(target_id=target.target_id)
    roles = [
        CheckpointRole.MEMBER_IDENTITY_VERIFIED,
        CheckpointRole.ACCOUNT_IDENTITY_VERIFIED,
        CheckpointRole.SOURCE_IDENTITY_VERIFIED,
        CheckpointRole.FILTERS_VERIFIED,
        CheckpointRole.PAGE_EXHAUSTED,
        CheckpointRole.SOURCE_EXHAUSTED,
        CheckpointRole.ACCOUNTS_EXHAUSTED,
    ]
    steps = [
        NavigateStep(
            step_id="navigate-entry",
            url=VariableValue(name="entry_url"),
            checks=[visible],
            checkpoint="root",
            checkpoint_role=CheckpointRole.MEMBER_IDENTITY_VERIFIED,
            provenance=Provenance(kind="observed", event_ids=["event-1"]),
        )
    ]
    for index, role in enumerate(roles[1:], start=2):
        steps.append(
            AssertStep(
                step_id=f"check-{index}",
                predicate=visible,
                checks=[visible],
                checkpoint=f"checkpoint-{index}",
                checkpoint_role=role,
                provenance=Provenance(kind="observed", event_ids=[f"event-{index}"]),
            )
        )
    steps.append(
        ReturnStep(
            step_id="return-not-found",
            result_kind="business_outcome",
            result=LiteralValue(value="NOT_FOUND"),
            checks=[visible],
            checkpoint=None,
            provenance=Provenance(kind="observed", event_ids=["event-8"]),
        )
    )
    event_ids = [f"event-{index}" for index in range(1, 9)]
    capability = Capability(
        schema_version="1.0",
        capability_id="trace_incoming_payment",
        capability_version="0.1.0",
        vendor="Northstar Synthetic Bank",
        supported_app_versions=["2026.09"],
        required_surface_features=["web", "frames", "accessible_roles"],
        input_schema=InputContract(),
        output_schema=OutputContract(result_kinds=["business_outcome"]),
        scope=CapabilityScope(
            direction="CREDIT",
            currency="USD",
            sources=["history", "pending"],
            effect="read_only",
            max_date_window_days=31,
        ),
        targets=[target],
        steps=steps,
        handlers=[],
        limits=ExecutionLimits(
            max_accounts=3,
            max_pages_per_source=3,
            max_rows_per_page=10,
            max_ui_actions=200,
            active_timeout_seconds=300,
            action_timeout_seconds=10,
            max_retries=2,
            retry_backoff_ms=[250, 1000],
            max_interventions=2,
            human_timeout_seconds=600,
        ),
        provenance=Provenance(kind="observed", event_ids=event_ids),
        validation=[],
    )
    initial = Observation(
        observation_id="observation-0",
        vendor="Northstar Synthetic Bank",
        app_version="2026.09",
        surface_features=["web", "frames", "accessible_roles"],
    )
    operations = []
    for index, step in enumerate(steps, start=1):
        operations.append(
            RecordedOperation(
                event_id=f"event-{index}",
                observation_id=f"observation-{index - 1}",
                resulting_observation_id=f"observation-{index}",
                step=step,
                target=None if index == 1 else target,
            )
        )
    recording = DiscoveryRecording(
        run_id="compile-test",
        goal="Trace incoming payment using symbolic inputs",
        initial_observation=initial,
        operations=operations,
        sensitive_values=("12345", "250.00"),
    )
    return capability, recording


def test_compilation_preserves_all_observed_events_and_never_freezes_query_values() -> None:
    proposal, recording = _candidate_and_recording()

    artifact = compile_candidate(recording, proposal.model_dump(mode="json"), _bindings())

    serialized = artifact.model_dump_json()
    assert '"12345"' not in serialized
    assert '"250.00"' not in serialized
    assert recording.event_ids <= set(artifact.provenance.event_ids)
    assert artifact.validation == []


def test_compiler_rejects_event_provenance_not_present_in_recording() -> None:
    proposal, recording = _candidate_and_recording()
    raw = proposal.model_dump(mode="json")
    raw["steps"][0]["provenance"]["event_ids"] = ["invented-event"]

    with pytest.raises(CompilationError, match="provenance"):
        compile_candidate(recording, raw, _bindings())


def test_compiler_rejects_frozen_query_value_anywhere_in_candidate() -> None:
    proposal, recording = _candidate_and_recording()
    raw = proposal.model_dump(mode="json")
    raw["supported_app_versions"].append("12345")

    with pytest.raises(CompilationError, match="input value"):
        compile_candidate(recording, raw, _bindings())


def test_compiler_rejects_missing_required_identity_checkpoint() -> None:
    proposal, recording = _candidate_and_recording()
    raw = deepcopy(proposal.model_dump(mode="json"))
    raw["steps"][0]["checkpoint_role"] = None

    with pytest.raises(CompilationError, match="member_identity_verified"):
        compile_candidate(recording, raw, _bindings())


def test_compiler_rejects_model_operations_outside_contract() -> None:
    proposal, recording = _candidate_and_recording()
    raw = proposal.model_dump(mode="json")
    raw["steps"][1]["kind"] = "run_javascript"

    with pytest.raises(CompilationError, match="schema"):
        compile_candidate(recording, raw, _bindings())


def test_compiler_rejects_target_that_keeps_only_the_recorded_role() -> None:
    proposal, recording = _candidate_and_recording()
    raw = proposal.model_dump(mode="json")
    raw["targets"][0]["strategies"][0]["name"] = {
        "kind": "variable",
        "name": "entry_url",
        "field": None,
    }

    with pytest.raises(CompilationError, match="target differs semantically"):
        compile_candidate(recording, raw, _bindings())


def test_compiler_rejects_target_that_drops_recorded_context() -> None:
    proposal, recording = _candidate_and_recording()
    recording.operations[1].target = recording.operations[1].target.model_copy(
        update={"container": "member-summary"}
    )

    with pytest.raises(CompilationError, match="target differs semantically"):
        compile_candidate(recording, proposal.model_dump(mode="json"), _bindings())


def test_compiler_allows_validation_bound_generalized_target() -> None:
    proposal, recording = _candidate_and_recording()
    generalized_target = TargetSpec(
        target_id="generalized-member-heading",
        surface_kind="web",
        frame_path=["Bank workspace"],
        strategies=[AccessibleRoleStrategy(role="heading", name="Member summary")],
    )
    proposal = proposal.model_copy(
        update={"targets": [*proposal.targets, generalized_target]}
    )
    proposal.steps[1] = proposal.steps[1].model_copy(
        update={
            "predicate": VisiblePredicate(target_id=generalized_target.target_id),
            "checks": [VisiblePredicate(target_id=generalized_target.target_id)],
            "provenance": Provenance(
                kind="generalized",
                event_ids=["event-2"],
                validation_scenario="member-summary",
            ),
        }
    )

    artifact = compile_candidate(recording, proposal, _bindings())

    assert artifact.steps[1].provenance.kind == "generalized"
