"""Authored interpreter tests; these are not evidence of model discovery."""

import asyncio
from datetime import UTC, datetime

import pytest

from casetrace.contracts import (
    Capability,
    FailureCode,
    Observation,
    ScenarioResult,
    TenantBindings,
    ValidationReport,
)
from casetrace.evidence import EvidenceWriter
from casetrace.integrity import binding_digest, capability_digest
from casetrace.replay import ReplayRunner, replay
from casetrace.session import SessionController, SessionState
from tests.unit.test_contracts import _artifact


class MemorySurface:
    def __init__(self, *, version="2026.09", checks=True):
        self.version = version
        self.checks = checks
        self.actions = []

    def bind_query(self, query):
        self.query = query

    def set_targets(self, targets):
        self.targets = targets

    async def observe(self):
        return Observation(
            observation_id="test-observation",
            vendor="CaseTrace Fixture Bank",
            app_version=self.version,
            surface_features=["web.frames", "web.tables"],
            state={"page_fingerprint": "test-page", "screen": "member_summary"},
        )

    async def act(self, step, args, variables):
        self.actions.append(step.step_id)

    async def read(self, step, args, variables):
        return args.member_id

    async def check(self, predicate, args, variables):
        return self.checks

    async def capture(self):
        return {"snapshot": {"state": {"screen": "authored-test"}}}


def bindings():
    return TenantBindings(
        tenant_id="test",
        vendor="CaseTrace Fixture Bank",
        origin="http://127.0.0.1:8000",
        app_version="2026.09",
        timezone="America/Chicago",
    )


def sealed(capability):
    report = ValidationReport(
        artifact_digest=capability_digest(capability),
        binding_digest=binding_digest(bindings()),
        scenario_results=[
            ScenarioResult(scenario="test_fixture", passed=True, evidence_refs=["test"])
        ],
        validated_at=datetime.now(UTC),
    )
    return capability.model_copy(update={"validation": [report]})


async def invoke(tmp_path, make_query, capability=None, surface=None, **kwargs):
    capability = capability or Capability.model_validate(_artifact())
    surface = surface or MemorySurface()
    return await replay(
        capability,
        make_query(),
        bindings(),
        surface,
        SessionController("test-run"),
        EvidenceWriter(tmp_path, "test-run"),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_replay_rejects_unvalidated_artifact_before_actions(tmp_path, make_query):
    surface = MemorySurface()
    result = await invoke(tmp_path, make_query, surface=surface)
    assert result.code == FailureCode.UNSUPPORTED_VERSION
    assert surface.actions == []


@pytest.mark.asyncio
async def test_incomplete_search_cannot_return_not_found(tmp_path, make_query):
    result = await invoke(tmp_path, make_query, validation_mode=True)
    assert result.kind == "failure"
    assert result.code == FailureCode.CHECKPOINT_FAILED


@pytest.mark.asyncio
async def test_live_version_mismatch_stops_before_mutation(tmp_path, make_query):
    surface = MemorySurface(version="unexpected")
    result = await invoke(tmp_path, make_query, surface=surface, validation_mode=True)
    assert result.code == FailureCode.UNSUPPORTED_VERSION
    assert surface.actions == []


@pytest.mark.asyncio
async def test_binding_or_artifact_changes_invalidate_seal(tmp_path, make_query):
    raw = _artifact()
    capability = sealed(Capability.model_validate(raw))
    capability = capability.model_copy(update={"vendor": "Other bank"})
    surface = MemorySurface()
    result = await invoke(tmp_path, make_query, capability, surface)
    assert result.kind == "failure"
    assert surface.actions == []


@pytest.mark.asyncio
async def test_false_postcondition_prevents_following_actions(tmp_path, make_query):
    surface = MemorySurface(checks=False)
    result = await invoke(tmp_path, make_query, surface=surface, validation_mode=True)
    assert result.code == FailureCode.CHECKPOINT_FAILED
    assert surface.actions == ["fill-member"]


@pytest.mark.asyncio
async def test_replay_events_explicitly_have_zero_model_calls(tmp_path, make_query):
    await invoke(tmp_path, make_query, validation_mode=True)
    import json

    events = [
        json.loads(line) for line in (tmp_path / "test-run/events.jsonl").read_text().splitlines()
    ]
    assert events
    assert all(event["model_call_count"] == 0 for event in events)
    assert not any(event.get("provider_response_id") for event in events)


@pytest.mark.asyncio
async def test_read_postcondition_can_reference_just_read_variable(tmp_path, make_query):
    raw = _artifact()
    raw["steps"][1]["checks"] = [
        {
            "kind": "equals",
            "left": {"kind": "variable", "name": "verified_member"},
            "right": {"kind": "input", "name": "member_id"},
        }
    ]
    surface = MemorySurface()
    checked = []

    async def check(predicate, args, variables):
        if predicate.kind == "equals":
            checked.append(variables["verified_member"])
        return True

    surface.check = check
    result = await invoke(
        tmp_path, make_query, Capability.model_validate(raw), surface, validation_mode=True
    )
    assert checked == [make_query().member_id, make_query().member_id]
    assert result.expected_condition == "complete consistent search"


@pytest.mark.asyncio
async def test_declared_handoff_keeps_session_and_restarts_after_verified_resume(
    tmp_path, make_query
):
    import asyncio

    from casetrace.session import SessionState

    raw = _artifact()
    raw["targets"].append(raw["targets"][0] | {"target_id": "auth"})
    raw["handlers"] = [
        {
            "handler_id": "login",
            "detector": {"kind": "visible", "target_id": "auth"},
            "disposition": "handoff",
            "failure_code": "ACCESS_DENIED",
            "provenance": raw["provenance"],
        }
    ]
    raw["steps"].insert(
        0,
        {
            "kind": "navigate",
            "step_id": "entry",
            "url": {"kind": "variable", "name": "entry_url"},
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "root",
            "provenance": raw["provenance"],
        },
    )
    surface = MemorySurface()
    surface.authenticated = False

    async def check(predicate, args, variables):
        if getattr(predicate, "target_id", None) == "auth":
            return not surface.authenticated
        return True

    surface.check = check

    async def verify():
        return surface.authenticated

    session = SessionController("test-run", resume_verifier=verify)
    ids = (session.browser_id, session.context_id, session.page_id)

    async def simulated_operator():
        async with asyncio.timeout(2):
            while session.state != SessionState.PAUSING:
                await asyncio.sleep(0.01)
            await session.take_control(session.intervention.id)
            surface.authenticated = True
            await session.resume(session.intervention.id)

    task = asyncio.create_task(simulated_operator())
    result = await replay(
        Capability.model_validate(raw),
        make_query(),
        bindings(),
        surface,
        session,
        EvidenceWriter(tmp_path, "test-run"),
        validation_mode=True,
    )
    await task
    assert result.code == FailureCode.CHECKPOINT_FAILED  # Still no search coverage in this test.
    assert ids == (session.browser_id, session.context_id, session.page_id)
    assert session.intervention.state == "resumed"
    assert surface.actions[0] == "entry"


def recovery_artifact(disposition="handoff", **limits):
    raw = _artifact()
    raw["targets"].append(raw["targets"][0] | {"target_id": "recovery"})
    raw["handlers"] = [
        {
            "handler_id": "recover",
            "detector": {"kind": "visible", "target_id": "recovery"},
            "disposition": disposition,
            "failure_code": "ACCESS_DENIED",
            "provenance": raw["provenance"],
        }
    ]
    if disposition == "retry":
        raw["handlers"][0].update(max_attempts=2, backoff_ms=[250, 1000])
    raw["steps"].insert(
        0,
        {
            "kind": "navigate",
            "step_id": "entry",
            "url": {"kind": "variable", "name": "entry_url"},
            "checks": [{"kind": "visible", "target_id": "member-input"}],
            "checkpoint": "root",
            "provenance": raw["provenance"],
        },
    )
    raw["limits"].update(limits)
    return Capability.model_validate(raw)


class RecoverySurface(MemorySurface):
    def __init__(self, *, delay=0, fail_count=0):
        super().__init__()
        self.recovery = True
        self.delay = delay
        self.fail_count = fail_count

    async def check(self, predicate, args, variables):
        if getattr(predicate, "target_id", None) == "recovery":
            return self.recovery
        return True

    async def act(self, step, args, variables):
        self.actions.append(step.step_id)
        await asyncio.sleep(self.delay)
        if self.fail_count:
            self.fail_count -= 1
            raise TimeoutError
        self.recovery = False


async def wait_for_handoff(session):
    async with asyncio.timeout(2):
        while session.state != SessionState.PAUSING:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("abort", [False, True])
async def test_handoff_timeout_and_abort_are_bounded(tmp_path, make_query, abort):
    session = SessionController("test-run")
    surface = RecoverySurface()
    task = asyncio.create_task(
        replay(
            recovery_artifact(human_timeout_seconds=1),
            make_query(),
            bindings(),
            surface,
            session,
            EvidenceWriter(tmp_path, "test-run"),
            validation_mode=True,
        )
    )
    await wait_for_handoff(session)
    if abort:
        await session.abort()
    result = await asyncio.wait_for(task, 2)
    assert result.code == (FailureCode.CANCELLED if abort else FailureCode.HANDOFF_TIMEOUT)
    assert session.state == SessionState.CANCELLED
    assert surface.actions == []


@pytest.mark.asyncio
async def test_human_wait_does_not_consume_active_budget(tmp_path, make_query):
    surface = RecoverySurface()

    async def verify():
        return not surface.recovery

    session = SessionController("test-run", resume_verifier=verify)
    task = asyncio.create_task(
        replay(
            recovery_artifact(active_timeout_seconds=1, human_timeout_seconds=3),
            make_query(),
            bindings(),
            surface,
            session,
            EvidenceWriter(tmp_path, "test-run"),
            validation_mode=True,
        )
    )
    await wait_for_handoff(session)
    await session.take_control(session.intervention.id)
    await asyncio.sleep(1.1)
    surface.recovery = False
    await session.resume(session.intervention.id)
    result = await task
    assert result.code == FailureCode.CHECKPOINT_FAILED
    assert surface.actions == ["entry", "fill-member"]


@pytest.mark.asyncio
async def test_active_work_still_times_out(tmp_path, make_query):
    surface = RecoverySurface(delay=1.5)
    surface.recovery = False
    result = await invoke(
        tmp_path,
        make_query,
        recovery_artifact(active_timeout_seconds=1),
        surface,
        validation_mode=True,
    )
    assert result.code == FailureCode.TIMEOUT
    assert surface.actions == ["entry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_count, expected_attempts", [(1, 1), (4, 2)])
async def test_only_declared_bounded_retry_recovers_primitive(
    tmp_path,
    make_query,
    fail_count,
    expected_attempts,
):
    surface = RecoverySurface(fail_count=fail_count)
    result = await invoke(
        tmp_path, make_query, recovery_artifact("retry"), surface, validation_mode=True
    )
    assert result.attempt_count == expected_attempts
    assert surface.actions.count("entry") == expected_attempts + 1
    assert result.code == (
        FailureCode.CHECKPOINT_FAILED if fail_count == 1 else FailureCode.TIMEOUT
    )


def test_payment_reduction_cannot_borrow_coverage_from_omitted_reads(tmp_path, make_query):
    from casetrace.contracts import PaymentDecisionBindings, ReturnStep, RunStopped

    capability = Capability.model_validate(_artifact())
    runner = ReplayRunner(
        capability,
        make_query(),
        bindings(),
        MemorySurface(),
        SessionController("test-run"),
        EvidenceWriter(tmp_path, "test-run"),
    )
    runner.accounts = {"A1"}
    runner.accounts_complete = True
    runner.exhausted = {("A1", "history"), ("A1", "pending")}
    runner.records["history-read"] = []
    runner.records["pending-read"] = []
    step = ReturnStep(
        step_id="decision",
        result_kind="payment_decision",
        result=PaymentDecisionBindings(record_steps=["history-read"], detail_steps=["detail-read"]),
        checks=[],
        checkpoint="terminal",
        provenance=capability.provenance,
    )
    with pytest.raises(RunStopped) as stopped:
        runner.terminal(step)
    assert stopped.value.code == FailureCode.CHECKPOINT_FAILED


@pytest.mark.asyncio
async def test_verified_resume_discards_prior_candidates_and_repeats_coverage(tmp_path, make_query):
    raw = recovery_artifact().model_dump(mode="json")
    provenance = raw["provenance"]
    checks = [{"kind": "visible", "target_id": "member-input"}]

    def step(kind, name, **fields):
        return {
            "kind": kind,
            "step_id": name,
            "checks": checks,
            "checkpoint": None,
            "provenance": provenance,
            **fields,
        }

    def read(name, **fields):
        return step(
            "read",
            name,
            target_id="member-input",
            store_as=name.replace("-", "_"),
            parser="fields",
            **fields,
        )

    def variable(name, field=None):
        return {"kind": "variable", "name": name, **({"field": field} if field else {})}

    sources = []
    for source in ("history", "pending"):
        rows = read(
            source + "-rows",
            read_role="payment_rows",
            account=variable("account", "account_id"),
            source={"kind": "literal", "value": source},
        )
        rows["parser"] = "table_rows"
        body = [read(source + "-context"), rows]
        if source == "history":
            body.append(
                step(
                    "for_each",
                    "detail-loop",
                    collection=variable("history_rows"),
                    item_variable="row",
                    max_items=10,
                    steps=[read("detail", read_role="payment_detail")],
                )
            )
        sources.append(
            step(
                "paginate",
                source + "-pages",
                next_target_id="next-page",
                max_pages=3,
                until={"kind": "absent", "target_id": "next-page"},
                steps=body,
            )
        )
    accounts = read("accounts", read_role="accounts")
    accounts["parser"] = "table_rows"
    raw["steps"] = [
        raw["steps"][0],
        read("identity"),
        accounts,
        step(
            "for_each",
            "account-loop",
            collection=variable("accounts"),
            item_variable="account",
            max_items=3,
            steps=sources,
        ),
        step(
            "return",
            "decision",
            result_kind="payment_decision",
            result={
                "kind": "payment_decision",
                "record_steps": ["history-rows", "pending-rows"],
                "detail_steps": ["detail"],
            },
        ),
    ]

    class ExpiringSurface(RecoverySurface):
        def __init__(self):
            super().__init__()
            self.recovery = False
            self.resumed = False
            self.reads = []

        async def read(self, step, args, variables):
            self.reads.append(step.step_id)
            record = args.model_dump(mode="json") | {
                "account_id": "A1",
                "source": "history",
                "reference": "OLD-CANDIDATE",
                "direction": "CREDIT",
                "transaction_date": "2026-09-03",
                "status": "POSTED",
            }
            if step.step_id == "identity":
                return {"member_id": args.member_id}
            if step.step_id == "accounts":
                return [{"account_id": "A1"}]
            if step.step_id == "detail":
                return record
            if step.read_role == "payment_rows":
                return [record] if step.step_id == "history-rows" and not self.resumed else []
            source = step.step_id.removesuffix("-context")
            if source == "pending" and not self.resumed:
                self.recovery = True
            return args.model_dump(mode="json") | {
                "account_id": "A1",
                "source": source,
                "direction": "CREDIT",
                "reference": "",
            }

    surface = ExpiringSurface()

    async def verify():
        return surface.resumed

    session = SessionController("test-run", resume_verifier=verify)
    task = asyncio.create_task(
        replay(
            Capability.model_validate(raw),
            make_query(),
            bindings(),
            surface,
            session,
            EvidenceWriter(tmp_path, "test-run"),
            validation_mode=True,
        )
    )
    await wait_for_handoff(session)
    assert "detail" in surface.reads
    await session.take_control(session.intervention.id)
    surface.recovery = False
    surface.resumed = True
    await session.resume(session.intervention.id)
    result = await task
    assert result.kind == "business_outcome", result
    assert result.code == "NOT_FOUND"
    assert surface.reads.count("accounts") == 2
    assert surface.reads.count("history-rows") == 2
    assert surface.reads.count("pending-rows") == 1
    assert result.coverage.accounts_complete
    assert result.coverage.sources_complete
    assert result.coverage.pages_searched == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"max_interventions": 0}, {"max_retries": 0}])
async def test_zero_recovery_budget_permits_no_recovery_action(tmp_path, make_query, change):
    disposition = "retry" if "max_retries" in change else "handoff"
    surface = RecoverySurface(fail_count=1)
    result = await invoke(
        tmp_path,
        make_query,
        recovery_artifact(disposition, **change),
        surface,
        validation_mode=True,
    )
    assert result.kind == "failure"
    assert result.attempt_count == 0
    assert surface.actions == (["entry"] if disposition == "retry" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits, expected",
    [
        ({"active_timeout_seconds": 1}, FailureCode.TIMEOUT),
        ({"max_ui_actions": 1}, FailureCode.SEARCH_LIMIT_EXCEEDED),
    ],
)
async def test_restarts_do_not_reset_active_or_action_budget(
    tmp_path, make_query, limits, expected
):
    class ExpiringActionSurface(RecoverySurface):
        async def act(self, step, args, variables):
            self.actions.append(step.step_id)
            await asyncio.sleep(0.6)
            self.recovery = True

    surface = ExpiringActionSurface()
    surface.recovery = False

    async def verify():
        return not surface.recovery

    session = SessionController("test-run", resume_verifier=verify)
    task = asyncio.create_task(
        replay(
            recovery_artifact(**limits),
            make_query(),
            bindings(),
            surface,
            session,
            EvidenceWriter(tmp_path, "test-run"),
            validation_mode=True,
        )
    )
    await wait_for_handoff(session)
    await session.take_control(session.intervention.id)
    surface.recovery = False
    await session.resume(session.intervention.id)
    result = await task
    assert result.code == expected


@pytest.mark.asyncio
async def test_handoff_transition_itself_is_bounded(tmp_path, make_query):
    async def transition(state):
        if state == SessionState.PAUSING:
            await asyncio.sleep(3)

    session = SessionController("test-run", transition_handler=transition)
    result = await asyncio.wait_for(
        replay(
            recovery_artifact(human_timeout_seconds=1),
            make_query(),
            bindings(),
            RecoverySurface(),
            session,
            EvidenceWriter(tmp_path, "test-run"),
            validation_mode=True,
        ),
        2,
    )
    assert result.code == FailureCode.HANDOFF_TIMEOUT
    assert session.state == SessionState.CANCELLED
