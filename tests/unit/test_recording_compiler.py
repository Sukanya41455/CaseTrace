from __future__ import annotations

import pytest

from casetrace.compiler import CompilationError
from casetrace.contracts import (
    AccessibleRoleStrategy,
    AssertStep,
    BranchStep,
    CheckpointRole,
    ClickStep,
    FillStep,
    ForEachStep,
    InputValue,
    LiteralValue,
    NavigateStep,
    Observation,
    PaginateStep,
    PaymentDecisionBindings,
    Provenance,
    ReadParser,
    ReadRole,
    ReadStep,
    ReturnStep,
    TableRelationStrategy,
    TargetSpec,
    TenantBindings,
    VariableValue,
    VisiblePredicate,
    VisibleTextStrategy,
)
from casetrace.discovery import DiscoveryRecording, RecordedOperation
from casetrace.integrity import capability_digest
from casetrace.recording_compiler import build_target_catalog, compile_recording, qualify_recording


def _bindings() -> TenantBindings:
    return TenantBindings(
        tenant_id="northstar-synthetic",
        vendor="Northstar Synthetic Bank",
        origin="http://127.0.0.1:8000",
        app_version="2026.09",
        timezone="America/Chicago",
    )


def _target(
    target_id: str, role: str, name: str, *, frame_path: list[str] | None = None
) -> TargetSpec:
    return TargetSpec(
        target_id=target_id,
        surface_kind="web",
        frame_path=frame_path or ["content"],
        strategies=[AccessibleRoleStrategy(role=role, name=name)],
    )


def _operation(event_id: str, step, target: TargetSpec | None) -> RecordedOperation:
    ordinal = event_id.removeprefix("event-")
    return RecordedOperation(
        event_id=event_id,
        observation_id=f"observation-{ordinal}",
        resulting_observation_id=f"observation-{int(ordinal) + 1}",
        step=step,
        target=target,
    )


def _complete_recording() -> DiscoveryRecording:
    authored = Provenance(kind="authored", validation_scenario="discovery-in-progress")
    member = _target("member-input", "textbox", "Member ID")
    search = _target("member-search", "button", "Search")
    account_open = _target("account-open", "button", "Open")
    amount = _target("amount-input", "textbox", "Amount")
    date_from = _target("date-from-input", "textbox", "Start date")
    date_to = _target("date-to-input", "textbox", "End date")
    currency = _target("currency-input", "textbox", "Currency")
    direction = _target("direction-input", "textbox", "Direction")
    apply = _target("apply-filters", "button", "Apply filters")
    activity = _target("activity-table", "table", "History activity")
    next_page = _target("next-page", "button", "Next")
    view = _target("transaction-view", "button", "View")
    detail = _target("transaction-detail", "table", "Transaction details")
    detail_visible = VisiblePredicate(target_id=detail.target_id)
    operations = [
        _operation(
            "event-1",
            NavigateStep(
                step_id="discovery-1",
                url=VariableValue(name="entry_url"),
                checks=[],
                checkpoint=None,
                provenance=authored,
            ),
            None,
        ),
        _operation(
            "event-2",
            FillStep(
                step_id="discovery-2",
                target_id=member.target_id,
                value=InputValue(name="member_id"),
                checks=[],
                checkpoint=None,
                provenance=authored,
            ),
            member,
        ),
        _operation(
            "event-3",
            ClickStep(
                step_id="discovery-3",
                target_id=search.target_id,
                checks=[],
                checkpoint=None,
                provenance=authored,
            ),
            search,
        ),
        _operation(
            "event-4",
            ClickStep(
                step_id="discovery-4",
                target_id=account_open.target_id,
                checks=[],
                checkpoint=None,
                provenance=authored,
            ),
            account_open,
        ),
    ]
    for ordinal, target, value in (
        (5, amount, InputValue(name="amount")),
        (6, date_from, InputValue(name="date_from")),
        (7, date_to, InputValue(name="date_to")),
        (8, currency, InputValue(name="currency")),
        (9, direction, LiteralValue(value="CREDIT")),
    ):
        operations.append(
            _operation(
                f"event-{ordinal}",
                FillStep(
                    step_id=f"discovery-{ordinal}",
                    target_id=target.target_id,
                    value=value,
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                target,
            )
        )
    operations.extend(
        [
            _operation(
                "event-10",
                ClickStep(
                    step_id="discovery-10",
                    target_id=apply.target_id,
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                apply,
            ),
            _operation(
                "event-11",
                ReadStep(
                    step_id="discovery-11",
                    target_id=activity.target_id,
                    parser=ReadParser.TABLE_ROWS,
                    read_role=ReadRole.PAYMENT_ROWS,
                    account=VariableValue(name="account"),
                    source=LiteralValue(value="history"),
                    store_as="payment_rows",
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                activity,
            ),
            _operation(
                "event-12",
                ClickStep(
                    step_id="discovery-12",
                    target_id=next_page.target_id,
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                next_page,
            ),
            _operation(
                "event-13",
                ReadStep(
                    step_id="discovery-13",
                    target_id=activity.target_id,
                    parser=ReadParser.TABLE_ROWS,
                    read_role=ReadRole.PAYMENT_ROWS,
                    account=VariableValue(name="account"),
                    source=LiteralValue(value="history"),
                    store_as="payment_rows",
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                activity,
            ),
            _operation(
                "event-14",
                ClickStep(
                    step_id="discovery-14",
                    target_id=view.target_id,
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                view,
            ),
            _operation(
                "event-15",
                ReadStep(
                    step_id="discovery-15",
                    target_id=detail.target_id,
                    parser=ReadParser.FIELDS,
                    read_role=ReadRole.PAYMENT_DETAIL,
                    store_as="payment_detail",
                    checks=[],
                    checkpoint=None,
                    provenance=authored,
                ),
                detail,
            ),
            _operation(
                "event-16",
                AssertStep(
                    step_id="discovery-16",
                    predicate=detail_visible,
                    checks=[detail_visible],
                    checkpoint="payment-confirmed",
                    checkpoint_role=CheckpointRole.PAYMENT_IDENTITY_VERIFIED,
                    provenance=authored,
                ),
                detail,
            ),
        ]
    )
    return DiscoveryRecording(
        run_id="qualified-recording",
        goal="Trace the incoming payment",
        initial_observation=Observation(
            observation_id="observation-0",
            vendor="Northstar Synthetic Bank",
            app_version="2026.09",
            surface_features=["web", "frames", "accessible_roles", "table_relationships"],
        ),
        operations=operations,
        sensitive_values=("12345", "250.00"),
    )


def test_qualifies_complete_ordered_payment_trace() -> None:
    trace = qualify_recording(_complete_recording(), _bindings())

    assert trace.navigate.event_id == "event-1"
    assert trace.member_fill.event_id == "event-2"
    assert [item.event_id for item in trace.payment_row_reads] == ["event-11", "event-13"]
    assert trace.detail_read.step.read_role is ReadRole.PAYMENT_DETAIL
    assert (
        trace.payment_confirmation.step.checkpoint_role
        is CheckpointRole.PAYMENT_IDENTITY_VERIFIED
    )


@pytest.mark.parametrize(
    ("event_id", "message"),
    [
        ("event-2", "member input"),
        ("event-10", "filter application"),
        ("event-15", "typed payment detail read"),
        ("event-16", "payment identity confirmation"),
    ],
)
def test_rejects_missing_required_trace_stage(event_id: str, message: str) -> None:
    recording = _complete_recording()
    recording.operations = [item for item in recording.operations if item.event_id != event_id]

    with pytest.raises(CompilationError, match=message):
        qualify_recording(recording, _bindings())


def test_rejects_incompatible_vendor_or_version() -> None:
    recording = _complete_recording()
    recording.initial_observation = recording.initial_observation.model_copy(
        update={"vendor": "Other Bank", "app_version": "1900.01"}
    )

    with pytest.raises(CompilationError, match="vendor or app version"):
        qualify_recording(recording, _bindings())


def test_qualifies_using_recognized_surface_when_initial_observation_is_unidentified() -> None:
    recording = _complete_recording()
    recording.initial_observation = recording.initial_observation.model_copy(
        update={"vendor": None, "app_version": None}
    )
    recording.recognize_surface(
        Observation(
            observation_id="observation-1",
            vendor="Northstar Synthetic Bank",
            app_version="2026.09",
            surface_features=["web", "frames", "accessible_roles"],
        )
    )

    trace = qualify_recording(recording, _bindings())

    assert trace.recording is recording


def test_target_catalog_renames_observed_targets_by_semantic_role() -> None:
    trace = qualify_recording(_complete_recording(), _bindings())
    targets = build_target_catalog(trace)

    assert targets["member"].strategies[0].name == "Member ID"
    assert targets["apply"].strategies[0].name == "Apply filters"
    assert targets["detail"].strategies[0].name == "Transaction details"
    assert targets["detail"].frame_path == ["content"]


def test_target_catalog_emits_explicit_generalized_targets() -> None:
    targets = build_target_catalog(qualify_recording(_complete_recording(), _bindings()))

    assert targets["member-not-found"].strategies == [
        VisibleTextStrategy(text="Member not found", exact=True)
    ]
    assert targets["accounts"].strategies[0].name == "Deposit accounts"
    assert isinstance(targets["history-view"].strategies[0], TableRelationStrategy)
    assert isinstance(targets["pending-view"].strategies[0], TableRelationStrategy)


def test_target_catalog_allows_single_page_recordings_without_next_control() -> None:
    recording = _complete_recording()
    recording.operations = [
        item for item in recording.operations if item.event_id != "event-12"
    ]

    capability = compile_recording(recording, _bindings())
    next_target = next(target for target in capability.targets if target.target_id == "next")

    assert next_target.strategies == [AccessibleRoleStrategy(role="button", name="Next")]


def test_target_catalog_rejects_conflicting_repeated_activity_targets() -> None:
    recording = _complete_recording()
    second = next(item for item in recording.operations if item.event_id == "event-13")
    second.target = second.target.model_copy(update={"frame_path": ["different-frame"]})

    with pytest.raises(CompilationError, match="activity target semantics"):
        build_target_catalog(qualify_recording(recording, _bindings()))


def _walk_steps(steps):
    for step in steps:
        yield step
        if isinstance(step, BranchStep):
            for case in step.cases:
                yield from _walk_steps(case.steps)
        elif isinstance(step, (ForEachStep, PaginateStep)):
            yield from _walk_steps(step.steps)


def test_compile_recording_emits_complete_deterministic_graph() -> None:
    recording = _complete_recording()

    first = compile_recording(recording, _bindings())
    second = compile_recording(recording, _bindings())

    assert capability_digest(first) == capability_digest(second)
    assert first.validation == []
    assert first.scope.sources == ["history", "pending"]
    roles = {
        step.checkpoint_role
        for step in _walk_steps(first.steps)
        if step.checkpoint_role is not None
    }
    assert roles == set(CheckpointRole)
    returns = [step for step in _walk_steps(first.steps) if isinstance(step, ReturnStep)]
    assert {step.result_kind for step in returns} == {"business_outcome", "payment_decision"}
    assert any(isinstance(step.result, PaymentDecisionBindings) for step in returns)


def test_generated_graph_separates_observed_and_generalized_provenance() -> None:
    capability = compile_recording(_complete_recording(), _bindings())
    steps = list(_walk_steps(capability.steps))

    observed = [step for step in steps if step.provenance.kind == "observed"]
    generalized = [step for step in steps if step.provenance.kind == "generalized"]
    assert observed
    assert generalized
    assert all(step.provenance.event_ids for step in observed)
    assert all(step.provenance.event_ids for step in generalized)
    assert all(step.provenance.validation_scenario for step in generalized)


def test_generated_graph_never_freezes_discovery_inputs() -> None:
    recording = _complete_recording()
    capability = compile_recording(recording, _bindings())
    serialized = capability.model_dump_json()

    assert all(value not in serialized for value in recording.sensitive_values)


def test_compilation_allows_the_fixed_currency_contract_value() -> None:
    recording = _complete_recording()
    recording.sensitive_values = (*recording.sensitive_values, "USD")

    capability = compile_recording(recording, _bindings())

    assert capability.scope.currency == "USD"
