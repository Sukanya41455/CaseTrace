"""Authored interpreter tests; these are not evidence of model discovery."""

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
from casetrace.replay import replay
from casetrace.session import SessionController
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
async def test_declared_handoff_keeps_session_and_restarts_after_verified_resume(tmp_path, make_query):
    import asyncio

    from casetrace.session import SessionState

    raw = _artifact()
    raw["targets"].append(raw["targets"][0] | {"target_id": "auth"})
    raw["handlers"] = [{
        "handler_id": "login", "detector": {"kind": "visible", "target_id": "auth"},
        "disposition": "handoff", "failure_code": "ACCESS_DENIED",
        "provenance": raw["provenance"],
    }]
    raw["steps"].insert(0, {
        "kind": "navigate", "step_id": "entry", "url": {"kind": "variable", "name": "entry_url"},
        "checks": [{"kind": "visible", "target_id": "member-input"}],
        "checkpoint": "root", "provenance": raw["provenance"],
    })
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
    result = await replay(Capability.model_validate(raw), make_query(), bindings(), surface,
                          session, EvidenceWriter(tmp_path, "test-run"), validation_mode=True)
    await task
    assert result.code == FailureCode.CHECKPOINT_FAILED  # Still no search coverage in this test.
    assert ids == (session.browser_id, session.context_id, session.page_id)
    assert session.intervention.state == "resumed"
    assert surface.actions[0] == "entry"
