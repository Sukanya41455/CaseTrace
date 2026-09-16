"""Authored UI traversal artifacts, deliberately separate from discovery evidence."""

import json
from pathlib import Path

import pytest

from casetrace.browser import BrowserSurface
from casetrace.contracts import (
    AccessibleRoleStrategy,
    AdjacentControlStrategy,
    Capability,
    FailureCode,
    TargetSpec,
    TenantBindings,
)
from casetrace.evidence import EvidenceWriter
from casetrace.recording_compiler import compile_recording
from casetrace.replay import replay
from casetrace.session import SessionController
from tests.harness import FixtureServer
from tests.integration.test_browser import _policy
from tests.unit.test_contracts import _artifact
from tests.unit.test_recording_compiler import _complete_recording


def authored_traversal(*, break_return=False, max_pages=3, omit_pending=False):
    """UI-only test recipe, authored independently of model discovery."""
    raw = _artifact()
    raw.update(vendor="Northstar Synthetic Bank", required_surface_features=["frames"])
    provenance = raw["provenance"]
    targets = []
    serial = 0

    def variable(name, field=None):
        return {"kind": "variable", "name": name, **({"field": field} if field else {})}

    def literal(value):
        return {"kind": "literal", "value": value}

    def target(name, strategy, container=None):
        targets.append(
            {
                "target_id": name,
                "surface_kind": "web",
                "frame_path": ["Bank workspace"],
                "strategies": [strategy],
                **({"container": container} if container else {}),
            }
        )

    def role(name, kind, label):
        target(name, {"kind": "accessible_role", "role": kind, "name": label})

    def step(kind, **fields):
        nonlocal serial
        serial += 1
        checks = (
            [{"kind": "visible", "target_id": fields["target_id"]}]
            if kind in {"fill", "read"}
            else []
        )
        return {
            "kind": kind,
            "step_id": f"step-{serial}",
            "checks": checks,
            "checkpoint": None,
            "provenance": provenance,
            **fields,
        }

    def click(target_id):
        expected = {
            "search": "accounts",
            "account-open": "context",
            "apply": "applied",
            "back-activity": "context",
            "back-member": "accounts",
        }.get(target_id, "detail" if target_id.endswith("-view") else "context")
        return step(
            "click", target_id=target_id, checks=[{"kind": "visible", "target_id": expected}]
        )

    def read(target_id, store_as, parser="fields", **fields):
        return step("read", target_id=target_id, parser=parser, store_as=store_as, **fields)

    def filters():
        return [
            step("fill", target_id=key, value={"kind": "input", "name": key})
            for key in ("date_from", "date_to", "amount", "currency")
        ] + [
            step("fill", target_id="direction", value=literal("CREDIT")),
            click("apply"),
        ]

    role("member", "textbox", "Member ID")
    role("search", "button", "Search")
    role("accounts", "table", "Deposit accounts")
    target("identity", {"kind": "visible_text", "text": "Member ID:", "exact": True})
    target("applied", {"kind": "visible_text", "text": "Applied filters", "exact": True})
    target(
        "permission-denied",
        {"kind": "visible_text", "text": "Permission denied", "exact": True},
    )
    target(
        "timeout-condition",
        {
            "kind": "visible_text",
            "text": "Timeout scenario: activity did not reach a normal checkpoint.",
            "exact": True,
        },
    )
    target(
        "account-open",
        {
            "kind": "table_relation",
            "header": "Account ID",
            "row_value": variable("account", "account_id"),
            "control_role": "link",
        },
        "Deposit accounts",
    )
    for name, label in [("date_from", "Start date"), ("date_to", "End date"), ("amount", "Amount")]:
        target(name, {"kind": "adjacent_control", "label": label, "control_role": "textbox"})
    role("currency", "combobox", "Currency")
    role("direction", "combobox", "Direction")
    role("apply", "button", "Apply filters")
    role("context", "table", "Verified account context")
    role("next", "link", "Next")
    role("detail", "table", "Transaction details")
    role("back-activity", "link", "Back to activity")
    role("back-member", "link", "Back to member")
    rows_ids, detail_ids, source_steps = [], [], []
    for source in ("history", "pending"):
        role(source, "link", source.capitalize())
        role(source + "-table", "table", source.capitalize() + " activity")
        target(
            source + "-view",
            {
                "kind": "table_relation",
                "header": "Reference",
                "row_value": variable(source + "_row", "reference"),
                "control_role": "link",
            },
            source.capitalize() + " activity",
        )
        rows = read(
            source + "-table",
            source + "_rows",
            "table_rows",
            read_role="payment_rows",
            account=variable("account", "account_id"),
            source=literal(source),
        )
        detail = read("detail", source + "_detail", read_role="payment_detail")
        rows_ids.append(rows["step_id"])
        detail_ids.append(detail["step_id"])
        restore = [click(source)] if break_return else []
        body = [
            read("context", source + "_context"),
            read("applied", source + "_filters"),
            rows,
            step(
                "for_each",
                collection=variable(source + "_rows"),
                item_variable=source + "_row",
                max_items=10,
                checks=[{"kind": "visible", "target_id": "context"}],
                steps=[click(source + "-view"), detail, click("back-activity"), *restore],
            ),
        ]
        source_steps += [
            click(source),
            *filters(),
            step(
                "paginate",
                next_target_id="next",
                max_pages=max_pages,
                until={"kind": "absent", "target_id": "next"},
                steps=body,
            ),
        ]
    raw["targets"] = targets
    raw["handlers"] = [
        {
            "handler_id": "permission-denied",
            "detector": {"kind": "visible", "target_id": "permission-denied"},
            "disposition": "fail",
            "failure_code": "ACCESS_DENIED",
            "provenance": provenance,
        },
        {
            "handler_id": "timeout-condition",
            "detector": {"kind": "visible", "target_id": "timeout-condition"},
            "disposition": "fail",
            "failure_code": "TIMEOUT",
            "provenance": provenance,
        },
    ]
    raw["steps"] = [
        step(
            "navigate",
            url=variable("entry_url"),
            checkpoint="entry",
            checks=[{"kind": "visible", "target_id": "member"}],
        ),
        step("fill", target_id="member", value={"kind": "input", "name": "member_id"}),
        click("search"),
        read("identity", "identity"),
        read("accounts", "accounts", "table_rows", read_role="accounts"),
        step(
            "for_each",
            collection=variable("accounts"),
            item_variable="account",
            max_items=3,
            checks=[{"kind": "visible", "target_id": "accounts"}],
            steps=[click("account-open"), *source_steps, click("back-member")],
        ),
        step(
            "return",
            result_kind="payment_decision",
            checkpoint="decision",
            checks=[{"kind": "visible", "target_id": "accounts"}],
            result={
                "kind": "payment_decision",
                "record_steps": rows_ids[:1] if omit_pending else rows_ids,
                "detail_steps": detail_ids,
            },
        ),
    ]
    return Capability.model_validate(raw)


async def run_authored(tmp_path, query, capability, scenario="normal"):
    with FixtureServer(scenario) as fixture:
        raw = json.loads(Path("config/base.json").read_text())
        bindings = TenantBindings.model_validate(raw["tenant_bindings"] | {"origin": fixture.url})
        surface = await BrowserSurface.launch(
            _policy(fixture.url), capability.targets, headless=True
        )
        try:
            session = SessionController(
                "authored-browser",
                browser_id=surface.browser_id,
                context_id=surface.context_id,
                page_id=surface.page_id,
            )
            result = await replay(
                capability,
                query,
                bindings,
                surface,
                session,
                EvidenceWriter(tmp_path, session.run_id),
                validation_mode=True,
            )
            events = [
                json.loads(line)
                for line in (tmp_path / session.run_id / "events.jsonl").read_text().splitlines()
            ]
            assert all(event["model_call_count"] == 0 for event in events)
            return result
        finally:
            await surface.close()


def _bindings() -> TenantBindings:
    return TenantBindings(
        tenant_id="northstar-synthetic",
        vendor="Northstar Synthetic Bank",
        origin="http://127.0.0.1:8000",
        app_version="2026.09",
        timezone="America/Chicago",
    )


def _complete_browser_recording():
    recording = _complete_recording()
    frame = ["Bank workspace"]

    def target(event_id, strategy, container=None):
        operation = next(item for item in recording.operations if item.event_id == event_id)
        operation.target = TargetSpec(
            target_id=operation.target.target_id,
            surface_kind="web",
            frame_path=frame,
            container=container,
            strategies=[strategy],
        )

    target("event-2", AccessibleRoleStrategy(role="textbox", name="Member ID"))
    target("event-3", AccessibleRoleStrategy(role="button", name="Search"))
    target("event-4", AccessibleRoleStrategy(role="link", name="Open"))
    target("event-5", AdjacentControlStrategy(label="Amount", control_role="textbox"))
    target("event-6", AdjacentControlStrategy(label="Start date", control_role="textbox"))
    target("event-7", AdjacentControlStrategy(label="End date", control_role="textbox"))
    target("event-8", AccessibleRoleStrategy(role="combobox", name="Currency"))
    target("event-9", AccessibleRoleStrategy(role="combobox", name="Direction"))
    target("event-10", AccessibleRoleStrategy(role="button", name="Apply filters"))
    activity = AccessibleRoleStrategy(role="table", name="History activity")
    target("event-11", activity)
    target("event-13", activity)
    target("event-12", AccessibleRoleStrategy(role="link", name="Next"))
    target("event-14", AccessibleRoleStrategy(role="link", name="View"))
    detail = AccessibleRoleStrategy(role="table", name="Transaction details")
    target("event-15", detail)
    target("event-16", detail)
    return recording


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_id", "amount", "kind", "value"),
    [
        ("12345", "250.00", "success", "POSTED"),
        ("12345", "125.00", "success", "PENDING"),
        ("12345", "80.00", "success", "REVERSED"),
        ("12345", "999.00", "business_outcome", "NOT_FOUND"),
        ("12345", "60.00", "business_outcome", "AMBIGUOUS"),
        ("99999", "250.00", "business_outcome", "MEMBER_NOT_FOUND"),
        ("11111", "250.00", "business_outcome", "NO_ACCOUNTS"),
    ],
)
async def test_compiled_recording_replays_all_required_outcomes(
    tmp_path, make_query, member_id, amount, kind, value
):
    capability = compile_recording(_complete_browser_recording(), _bindings())

    result = await run_authored(
        tmp_path,
        make_query(member_id=member_id, amount=amount),
        capability,
    )

    assert result.kind == kind, (
        result.current_step,
        result.expected_condition,
        result.observed_condition,
    )
    if kind == "success":
        assert result.payment.status == value
    else:
        assert result.code == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount, status",
    [
        ("250.00", "POSTED"),
        ("125.00", "PENDING"),
        ("80.00", "REVERSED"),
    ],
)
async def test_authored_browser_traversal_finds_status_after_complete_search(
    tmp_path,
    make_query,
    amount,
    status,
):
    result = await run_authored(tmp_path, make_query(amount=amount), authored_traversal())
    assert result.kind == "success", result
    assert result.payment.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("amount, outcome", [("999.00", "NOT_FOUND"), ("60.00", "AMBIGUOUS")])
async def test_authored_browser_traversal_exhausts_every_account_and_source(
    tmp_path,
    make_query,
    amount,
    outcome,
):
    result = await run_authored(tmp_path, make_query(amount=amount), authored_traversal())
    assert result.kind == "business_outcome", result
    assert result.code == outcome
    assert result.coverage.accounts_searched == 2
    assert result.coverage.pages_searched == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change, expected",
    [
        ({"max_pages": 1}, FailureCode.SEARCH_LIMIT_EXCEEDED),
        ({"omit_pending": True}, FailureCode.CHECKPOINT_FAILED),
    ],
)
async def test_authored_partial_or_subset_search_fails(tmp_path, make_query, change, expected):
    result = await run_authored(tmp_path, make_query(amount="999.00"), authored_traversal(**change))
    assert result.kind == "failure"
    assert result.code == expected


@pytest.mark.asyncio
async def test_detail_return_must_restore_same_page(tmp_path, make_query):
    result = await run_authored(tmp_path, make_query(), authored_traversal(break_return=True))
    assert result.kind == "failure"
    assert result.code == FailureCode.CHECKPOINT_FAILED
    assert result.expected_condition == "return to the same observed page before pagination"


@pytest.mark.asyncio
async def test_visible_permission_denial_returns_typed_failure(tmp_path, make_query):
    result = await run_authored(
        tmp_path,
        make_query(),
        authored_traversal(),
        scenario="permission-denied",
    )
    assert result.kind == "failure"
    assert result.code == FailureCode.ACCESS_DENIED


@pytest.mark.asyncio
async def test_visible_timeout_condition_returns_typed_failure(tmp_path, make_query):
    result = await run_authored(
        tmp_path,
        make_query(),
        authored_traversal(),
        scenario="timeout",
    )
    assert result.kind == "failure"
    assert result.code == FailureCode.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("unknown-dialog", FailureCode.UNKNOWN_STATE),
        ("stale-member", FailureCode.CHECKPOINT_FAILED),
        ("conflicting-record", FailureCode.INCONSISTENT_RECORD),
        ("search-limit", FailureCode.SEARCH_LIMIT_EXCEEDED),
    ],
)
async def test_authored_browser_traversal_returns_typed_fixture_failure(
    tmp_path,
    make_query,
    scenario,
    expected,
):
    result = await run_authored(
        tmp_path,
        make_query(),
        authored_traversal(),
        scenario=scenario,
    )
    assert result.kind == "failure"
    assert result.code == expected
