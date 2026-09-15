import json
from datetime import UTC, date, datetime

import pytest
from pydantic import TypeAdapter, ValidationError
from typer.testing import CliRunner

from casetrace.contracts import (
    BusinessOutcome,
    Capability,
    Failure,
    InputValue,
    Intervention,
    LiteralValue,
    PaymentQuery,
    PaymentRecord,
    Provenance,
    RunEvent,
    RunResult,
    Success,
    VariableValue,
    resolve_value,
)

BASE = {
    "member_id": "12345",
    "amount": "250.00",
    "currency": "USD",
    "date_from": "2026-09-01",
    "date_to": "2026-09-07",
    "reference": None,
}


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "250.001"},
        {"amount": "-1.00"},
        {"amount": "0.00"},
        {"amount": "NaN"},
        {"amount": "sNaN"},
        {"amount": "Infinity"},
        {"member_id": "12"},
        {"currency": "EUR"},
        {"date_to": "2026-08-31"},
        {"date_to": "2026-10-15"},
        {"date_from": "2026-09-01T00:00:00Z"},
        {"date_from": 0, "date_to": 0},
        {"reference": ""},
        {"undeclared": True},
    ],
)
def test_rejects_invalid_query(change):
    with pytest.raises(ValidationError):
        PaymentQuery.model_validate(BASE | change)


def test_member_id_preserves_zeroes(make_query):
    query = make_query(member_id="00123")
    assert query.member_id == "00123"


def test_query_preserves_exact_amount_and_trims_reference(make_query):
    query = make_query(reference="  PAY-123  ")
    assert query.amount == "250.00"
    assert query.reference == "PAY-123"
    assert query.date_from == date(2026, 9, 1)


def test_query_accepts_a_31_day_inclusive_window(make_query):
    query = make_query(date_from="2026-09-01", date_to="2026-10-01")
    assert query.date_to == date(2026, 10, 1)


def test_value_references_resolve_without_expression_evaluation(make_query):
    query = make_query()
    assert resolve_value(LiteralValue(value="USD"), query, {}) == "USD"
    assert resolve_value(InputValue(name="member_id"), query, {}) == "12345"
    assert resolve_value(VariableValue(name="account_id"), query, {"account_id": "A-7"}) == "A-7"

    with pytest.raises(KeyError):
        resolve_value(VariableValue(name="missing"), query, {})


def test_variable_reference_can_select_one_normalized_row_field(make_query):
    query = make_query()
    reference = VariableValue(name="account", field="account_id")
    assert resolve_value(reference, query, {"account": {"account_id": "A-7"}}) == "A-7"

    with pytest.raises(TypeError, match="mapping"):
        resolve_value(reference, query, {"account": "A-7"})


def _artifact() -> dict:
    provenance = {
        "kind": "authored",
        "event_ids": [],
        "validation_scenario": "test_fixture",
    }
    return {
        "schema_version": "1.0",
        "capability_id": "trace_incoming_payment",
        "capability_version": "0.1.0",
        "vendor": "CaseTrace Fixture Bank",
        "supported_app_versions": ["2026.09"],
        "required_surface_features": ["web.frames", "web.tables"],
        "input_schema": {
            "name": "PaymentQuery",
            "version": "1.0",
            "schema_ref": "#/$defs/PaymentQuery",
        },
        "output_schema": {
            "name": "RunResult",
            "version": "1.0",
            "schema_ref": "#/$defs/RunResult",
            "result_kinds": ["success", "business_outcome", "failure"],
        },
        "scope": {
            "direction": "CREDIT",
            "currency": "USD",
            "sources": ["history", "pending"],
            "effect": "read_only",
            "max_date_window_days": 31,
        },
        "targets": [
            {
                "target_id": "member-input",
                "surface_kind": "web",
                "frame_path": ["content"],
                "container": "member-search",
                "anchor": "Member ID",
                "strategies": [{"kind": "accessible_role", "role": "textbox", "name": "Member ID"}],
                "cardinality": 1,
            },
            {
                "target_id": "next-page",
                "surface_kind": "web",
                "frame_path": ["content"],
                "container": "activity",
                "anchor": "Transactions",
                "strategies": [{"kind": "accessible_role", "role": "link", "name": "Next"}],
                "cardinality": 1,
            },
        ],
        "steps": [
            {
                "kind": "fill",
                "step_id": "fill-member",
                "target_id": "member-input",
                "value": {"kind": "input", "name": "member_id"},
                "checks": [{"kind": "visible", "target_id": "member-input"}],
                "checkpoint": "member-query-filled",
                "provenance": provenance,
            },
            {
                "kind": "read",
                "step_id": "read-member",
                "target_id": "member-input",
                "parser": "text",
                "store_as": "verified_member",
                "checks": [{"kind": "visible", "target_id": "member-input"}],
                "checkpoint": None,
                "provenance": provenance,
            },
            {
                "kind": "return",
                "step_id": "return-not-found",
                "result_kind": "business_outcome",
                "result": {"kind": "literal", "value": "NOT_FOUND"},
                "checks": [
                    {
                        "kind": "equals",
                        "left": {"kind": "variable", "name": "verified_member"},
                        "right": {"kind": "input", "name": "member_id"},
                    }
                ],
                "checkpoint": "terminal-identity-check",
                "provenance": provenance,
            },
        ],
        "handlers": [],
        "limits": {
            "max_accounts": 3,
            "max_pages_per_source": 3,
            "max_rows_per_page": 10,
            "max_ui_actions": 200,
            "active_timeout_seconds": 300,
            "action_timeout_seconds": 10,
            "max_retries": 2,
            "retry_backoff_ms": [250, 1000],
            "max_interventions": 2,
            "human_timeout_seconds": 600,
        },
        "provenance": provenance,
        "validation": [],
    }


def test_accepts_bounded_typed_artifact():
    capability = Capability.model_validate(_artifact())
    assert capability.steps[0].kind == "fill"
    assert capability.provenance.validation_scenario == "test_fixture"


def test_rejects_unknown_capability_version():
    artifact = _artifact()
    artifact["capability_version"] = "0.2.0"
    with pytest.raises(ValidationError, match="capability_version"):
        Capability.model_validate(artifact)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda artifact: artifact.update(schema_version="2.0"), "schema_version"),
        (
            lambda artifact: [step.update(checkpoint=None) for step in artifact["steps"]],
            "checkpoint",
        ),
        (
            lambda artifact: artifact["steps"][0].update(target_id="does-not-exist"),
            "unknown target",
        ),
        (
            lambda artifact: artifact["steps"][-1]["checks"][0]["left"].update(
                name="does_not_exist"
            ),
            "unknown variable",
        ),
        (
            lambda artifact: artifact["steps"].insert(
                -1,
                {
                    "kind": "paginate",
                    "step_id": "pages",
                    "next_target_id": "next-page",
                    "until": {"kind": "absent", "target_id": "next-page"},
                    "steps": [],
                    "checks": [],
                    "checkpoint": "page-complete",
                    "provenance": artifact["provenance"],
                },
            ),
            "max_pages",
        ),
        (
            lambda artifact: artifact["steps"].insert(
                0,
                {
                    "kind": "python",
                    "step_id": "arbitrary-code",
                    "source": "open('secret')",
                    "checks": [],
                    "checkpoint": "bad",
                    "provenance": artifact["provenance"],
                },
            ),
            "tag",
        ),
    ],
)
def test_rejects_unsafe_or_incomplete_artifact(mutate, message):
    artifact = _artifact()
    mutate(artifact)
    with pytest.raises(ValidationError, match=message):
        Capability.model_validate(artifact)


def test_rejects_two_identical_branch_guards():
    artifact = _artifact()
    guard = {"kind": "visible", "target_id": "member-input"}
    artifact["steps"].insert(
        -1,
        {
            "kind": "branch",
            "step_id": "duplicate-branch",
            "cases": [
                {"guard": guard, "steps": []},
                {"guard": guard, "steps": []},
            ],
            "fallback_failure": "UNKNOWN_STATE",
            "checks": [],
            "checkpoint": "branch-selected",
            "provenance": artifact["provenance"],
        },
    )
    with pytest.raises(ValidationError, match="branch guards must be disjoint"):
        Capability.model_validate(artifact)


def test_rejects_return_kind_outside_declared_output_contract():
    artifact = _artifact()
    artifact["output_schema"]["result_kinds"] = ["success"]
    with pytest.raises(ValidationError, match="return kind"):
        Capability.model_validate(artifact)


def test_rejects_label_only_checkpoints_without_executable_checks():
    artifact = _artifact()
    for step in artifact["steps"]:
        step["checks"] = []
    with pytest.raises(ValidationError, match="executable check"):
        Capability.model_validate(artifact)


def test_rejects_terminal_branch_path_that_bypasses_checks():
    artifact = _artifact()
    artifact["steps"].insert(
        -1,
        {
            "kind": "branch",
            "step_id": "unchecked-terminal-branch",
            "cases": [
                {
                    "guard": {"kind": "visible", "target_id": "member-input"},
                    "steps": [
                        {
                            "kind": "return",
                            "step_id": "unchecked-return",
                            "result_kind": "business_outcome",
                            "result": {"kind": "literal", "value": "NOT_FOUND"},
                            "checks": [],
                            "checkpoint": "label-only",
                            "provenance": artifact["provenance"],
                        }
                    ],
                }
            ],
            "fallback_failure": "UNKNOWN_STATE",
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "branch-selected",
            "provenance": artifact["provenance"],
        },
    )
    with pytest.raises(ValidationError, match="return operation requires an executable check"):
        Capability.model_validate(artifact)


@pytest.mark.parametrize(
    "result",
    [
        {"kind": "input", "name": "amount"},
        {"kind": "variable", "name": "verified_member"},
    ],
)
def test_rejects_untyped_success_return_sources(result):
    artifact = _artifact()
    artifact["steps"][-1].update(result_kind="success", result=result)
    with pytest.raises(ValidationError, match="PaymentFieldBindings"):
        Capability.model_validate(artifact)


def _payment_bindings(variable: str = "payment_detail") -> dict:
    fields = (
        "member_id",
        "account_id",
        "reference",
        "direction",
        "amount",
        "currency",
        "transaction_date",
        "status",
        "source",
        "observation_id",
    )
    return {field: {"kind": "variable", "name": variable, "field": field} for field in fields}


def test_accepts_success_bindings_from_typed_payment_detail_read():
    artifact = _artifact()
    artifact["steps"][1].update(
        parser="fields",
        read_role="payment_detail",
        store_as="payment_detail",
        checks=[{"kind": "visible", "target_id": "member-input"}],
    )
    artifact["steps"][-1].update(
        result_kind="success",
        result=_payment_bindings(),
        checks=[
            {
                "kind": "equals",
                "left": {"kind": "variable", "name": "payment_detail", "field": "member_id"},
                "right": {"kind": "input", "name": "member_id"},
            }
        ],
        checkpoint_role="payment_identity_verified",
    )
    capability = Capability.model_validate(artifact)
    assert capability.steps[-1].result.member_id.field == "member_id"


def test_rejects_handler_without_validated_provenance():
    artifact = _artifact()
    handler = {
        "handler_id": "retry-timeout",
        "detector": {"kind": "visible", "target_id": "member-input"},
        "disposition": "retry",
        "failure_code": "TIMEOUT",
        "max_attempts": 2,
        "backoff_ms": [250, 1000],
    }
    artifact["handlers"] = [handler]
    with pytest.raises(ValidationError, match="provenance"):
        Capability.model_validate(artifact)

    handler["provenance"] = {"kind": "authored", "event_ids": []}
    with pytest.raises(ValidationError, match="validation scenario"):
        Capability.model_validate(artifact)


def test_rejects_ambiguous_next_page_target():
    artifact = _artifact()
    artifact["targets"][1]["cardinality"] = 2
    artifact["steps"].insert(
        -1,
        {
            "kind": "paginate",
            "step_id": "pages",
            "next_target_id": "next-page",
            "until": {"kind": "absent", "target_id": "next-page"},
            "max_pages": 3,
            "steps": [],
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "pages-exhausted",
            "provenance": artifact["provenance"],
        },
    )
    with pytest.raises(ValidationError, match="pagination target.*cardinality exactly one"):
        Capability.model_validate(artifact)


def test_rejects_variable_reference_before_producer():
    artifact = _artifact()
    artifact["steps"][0]["value"] = {"kind": "variable", "name": "verified_member"}
    with pytest.raises(ValidationError, match="before it is available"):
        Capability.model_validate(artifact)


def test_rejects_variable_from_only_one_branch_after_join():
    artifact = _artifact()
    branch_read = artifact["steps"].pop(1)
    branch_read["store_as"] = "branch_member"
    artifact["steps"].insert(
        1,
        {
            "kind": "branch",
            "step_id": "member-branch",
            "cases": [
                {
                    "guard": {"kind": "visible", "target_id": "member-input"},
                    "steps": [branch_read],
                },
                {
                    "guard": {"kind": "absent", "target_id": "member-input"},
                    "steps": [],
                },
            ],
            "fallback_failure": "UNKNOWN_STATE",
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "branch-complete",
            "provenance": artifact["provenance"],
        },
    )
    artifact["steps"][-1]["checks"][0]["left"] = {
        "kind": "variable",
        "name": "branch_member",
    }
    with pytest.raises(ValidationError, match="before it is available"):
        Capability.model_validate(artifact)


def test_rejects_loop_variable_outside_loop_body():
    artifact = _artifact()
    artifact["steps"][1].update(
        parser="table_rows",
        read_role="accounts",
        store_as="accounts",
        checks=[{"kind": "visible", "target_id": "member-input"}],
    )
    artifact["steps"].insert(
        -1,
        {
            "kind": "for_each",
            "step_id": "accounts-loop",
            "collection": {"kind": "variable", "name": "accounts"},
            "item_variable": "account",
            "max_items": 3,
            "steps": [],
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "accounts-finished",
            "provenance": artifact["provenance"],
        },
    )
    artifact["steps"][-1]["checks"][0]["left"] = {
        "kind": "variable",
        "name": "account",
        "field": "account_id",
    }
    with pytest.raises(ValidationError, match="before it is available"):
        Capability.model_validate(artifact)


def test_allows_trusted_entry_url_runtime_variable():
    artifact = _artifact()
    artifact["steps"].insert(
        0,
        {
            "kind": "navigate",
            "step_id": "open-entry",
            "url": {"kind": "variable", "name": "entry_url"},
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "entry-open",
            "provenance": artifact["provenance"],
        },
    )
    capability = Capability.model_validate(artifact)
    assert capability.steps[0].kind == "navigate"


def test_cli_exports_capability_json_schema(tmp_path):
    from casetrace.cli import app

    output = tmp_path / "schema.json"
    result = CliRunner().invoke(app, ["schema", "--output", str(output)])

    assert result.exit_code == 0, result.output
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["$id"] == "urn:casetrace:schema:1.0"
    assert exported["$defs"]["PaymentQuery"]["properties"]["member_id"]
    assert exported["$defs"]["RunResult"]["discriminator"]["propertyName"] == "kind"
    capability = exported["$defs"]["Capability"]
    assert capability["properties"]["input_schema"]["$ref"]
    assert exported["$defs"]["InputContract"]["properties"]["schema_ref"]["const"] == (
        "#/$defs/PaymentQuery"
    )


def test_terminal_results_are_discriminated_and_forbid_extra_fields():
    success = {
        "kind": "success",
        "run_id": "run-1",
        "artifact_digest": "a" * 64,
        "binding_digest": "b" * 64,
        "evidence_refs": ["events.jsonl#event-1"],
        "payment": {
            "member_id": "12345",
            "account_id": "A-100",
            "reference": "PAY-123",
            "direction": "CREDIT",
            "amount": "250.00",
            "currency": "USD",
            "transaction_date": "2026-09-03",
            "status": "POSTED",
            "source": "history",
            "observation_id": "obs-1",
        },
        "observed_at": "2026-09-11T12:00:00Z",
    }
    result = TypeAdapter(RunResult).validate_python(success)
    assert isinstance(result, Success)

    with pytest.raises(ValidationError):
        TypeAdapter(RunResult).validate_python(success | {"secret": "must fail"})


def test_observed_records_represent_distractors_but_success_rejects_them():
    distractor = PaymentRecord.model_validate(
        {
            "member_id": "12345",
            "account_id": "A-100",
            "reference": "PAY-OTHER",
            "direction": "DEBIT",
            "amount": "250.00",
            "currency": "EUR",
            "transaction_date": "2026-09-03",
            "status": "POSTED",
            "source": "history",
            "observation_id": "obs-1",
        }
    )
    assert distractor.direction == "DEBIT"
    assert distractor.currency == "EUR"

    with pytest.raises(ValidationError, match="CREDIT/USD"):
        Success.model_validate(
            {
                "kind": "success",
                "run_id": "run-1",
                "artifact_digest": "a" * 64,
                "binding_digest": "b" * 64,
                "evidence_refs": [],
                "payment": distractor,
                "observed_at": "2026-09-11T12:00:00Z",
            }
        )


def test_business_and_failure_results_require_execution_metadata():
    common = {
        "run_id": "run-1",
        "artifact_digest": "a" * 64,
        "binding_digest": "b" * 64,
        "evidence_refs": [],
    }
    coverage = {
        "accounts_complete": True,
        "sources_complete": True,
        "pages_complete": True,
        "accounts_searched": 1,
        "pages_searched": 2,
    }
    business = BusinessOutcome.model_validate(
        common | {"kind": "business_outcome", "code": "NOT_FOUND", "coverage": coverage}
    )
    failure = Failure.model_validate(
        common
        | {
            "kind": "failure",
            "code": "TIMEOUT",
            "current_step": "read-member",
            "expected_condition": "member detail visible",
            "observed_condition": "loading indicator visible",
            "attempt_count": 2,
        }
    )
    assert business.coverage.complete is True
    assert failure.code == "TIMEOUT"


def test_event_metadata_is_allowlisted_and_actor_is_explicit():
    event = RunEvent.model_validate(
        {
            "event_id": "event-1",
            "run_id": "run-1",
            "seq": 1,
            "timestamp": datetime(2026, 9, 11, 12, tzinfo=UTC),
            "kind": "action",
            "actor": "automation",
            "summary": "Opened the member search screen",
            "browser_id": "browser-1",
            "context_id": "context-1",
            "page_id": "page-1",
            "ownership_generation": 0,
        }
    )
    assert event.seq == 1

    with pytest.raises(ValidationError):
        RunEvent.model_validate(event.model_dump() | {"payload": {"raw": "secret"}})


def test_authored_provenance_and_event_defaults_support_runtime_call_sites():
    provenance = Provenance(kind="authored", validation_scenario="normal")
    event = RunEvent(
        event_id="event-1",
        run_id="run-1",
        seq=0,
        timestamp=datetime(2026, 9, 11, 12, tzinfo=UTC),
        kind="checkpoint",
        actor="automation",
        summary="Member identity verified",
    )
    assert provenance.event_ids == []
    assert event.ownership_generation == 0


def test_run_event_accepts_ollama_tag_as_model_id():
    event = RunEvent(
        event_id="event-1",
        run_id="run-1",
        seq=0,
        timestamp=datetime(2026, 9, 13, 12, tzinfo=UTC),
        kind="action",
        actor="automation",
        summary="Local model selected one action",
        model_id="qwen3.5:4b",
    )

    assert event.model_id == "qwen3.5:4b"


def test_run_event_accepts_namespaced_hosted_model_id():
    event = RunEvent(
        event_id="event-1",
        run_id="run-1",
        seq=0,
        timestamp=datetime(2026, 9, 15, 12, tzinfo=UTC),
        kind="action",
        actor="automation",
        summary="Hosted model selected one action",
        model_id="nex-agi/nex-n2.5-pro:free",
    )

    assert event.model_id == "nex-agi/nex-n2.5-pro:free"


def test_intervention_exposes_nonserialized_id_alias():
    intervention = Intervention.model_validate(
        {
            "intervention_id": "intervention-1",
            "run_id": "run-1",
            "state": "waiting_human",
            "reason": "Session authentication expired",
            "step_id": "member-search",
            "ownership_generation": 1,
            "created_at": datetime(2026, 9, 11, 12, tzinfo=UTC),
        }
    )
    assert intervention.id == "intervention-1"
    assert "id" not in intervention.model_dump()
