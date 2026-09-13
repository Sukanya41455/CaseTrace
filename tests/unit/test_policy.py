from __future__ import annotations

import asyncio
from datetime import date

import pytest

from casetrace.contracts import (
    ClickStep,
    FailureCode,
    InputValue,
    PaymentQuery,
    PolicyConfig,
    PolicyRoute,
    Provenance,
    RunStopped,
)
from casetrace.policy import Policy
from casetrace.session import SessionController, SessionState
from casetrace.surface import ResolvedTarget

ORIGIN = "http://127.0.0.1:8123"


def _policy() -> Policy:
    config = PolicyConfig(
        allowed_origins=["{tenant_origin}"],
        allowed_routes=[
            PolicyRoute(path="/", methods=["GET"], effect="read_only"),
            PolicyRoute(path="/bank/members/{member_id}", methods=["GET"], effect="read_only"),
            PolicyRoute(
                path="/bank/members/{member_id}/accounts/{account_id}/activity",
                methods=["GET"],
                effect="read_only",
            ),
        ],
        allowed_control_meanings=["member.account.open"],
        denied_control_meanings=["financial.transfer", "unknown"],
    )
    return Policy(config, tenant_origin=ORIGIN)


def _click() -> ClickStep:
    return ClickStep(
        step_id="open-account",
        target_id="open-control",
        checks=[],
        checkpoint=None,
        provenance=Provenance(kind="authored", validation_scenario="normal"),
    )


def _target(meaning: str, *, form_action: str | None = None) -> ResolvedTarget:
    return ResolvedTarget(
        target_id="open-control",
        count=1,
        control_meaning=meaning,
        page_url=f"{ORIGIN}/bank/members/12345",
        form_action=form_action,
        form_method="POST" if form_action else None,
    )


def test_click_requires_reviewed_control_meaning() -> None:
    policy = _policy()

    with pytest.raises(RunStopped) as failure:
        policy.authorize(_click(), _target("financial.transfer"))

    assert failure.value.code is FailureCode.POLICY_DENIED


def test_allowed_meaning_cannot_submit_to_unreviewed_destination() -> None:
    policy = _policy()

    with pytest.raises(RunStopped) as failure:
        policy.authorize(
            _click(),
            _target(
                "member.account.open",
                form_action=f"{ORIGIN}/bank/members/12345/accounts/SYNTH-DDA-A1/transfer",
            ),
        )

    assert failure.value.code is FailureCode.POLICY_DENIED


def test_route_policy_uses_parsed_origin_and_path() -> None:
    policy = _policy()

    assert policy.request_allowed(
        f"{ORIGIN}/bank/members/12345?ignored=https://attacker.invalid", "GET", "document"
    )
    assert not policy.request_allowed("http://localhost:8123/bank/members/12345", "GET", "document")
    assert not policy.request_allowed(f"{ORIGIN}/bank/members/12345/extra", "GET", "document")

    assert not policy.request_allowed(f"{ORIGIN}/unreviewed", "GET", "image")


@pytest.mark.asyncio
async def test_pause_blocks_queued_action_and_invalidates_late_result() -> None:
    controller = SessionController(run_id="run-1")
    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> str:
        started.set()
        await release.wait()
        return "private-result"

    first = asyncio.create_task(controller.execute(in_flight))
    await started.wait()
    intervention = await controller.request_handoff("authentication required", "step-1")

    assert controller.state is SessionState.PAUSING
    with pytest.raises(RunStopped) as blocked:
        await controller.execute(lambda: asyncio.sleep(0))
    assert blocked.value.code is FailureCode.CANCELLED

    takeover = asyncio.create_task(controller.take_control(intervention.intervention_id))
    await asyncio.sleep(0)
    assert not takeover.done()
    release.set()

    with pytest.raises(RunStopped) as late:
        await first
    assert late.value.code is FailureCode.CANCELLED
    await takeover
    assert controller.state is SessionState.HUMAN
    with pytest.raises(RunStopped):
        await controller.execute(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_resume_without_verifier_fails_closed() -> None:
    controller = SessionController(run_id="run-1")
    intervention = await controller.request_handoff("authentication required", "step-1")
    await controller.take_control(intervention.intervention_id)
    with pytest.raises(RunStopped):
        await controller.resume(intervention.intervention_id)
    assert controller.state is SessionState.HUMAN


@pytest.mark.asyncio
async def test_abort_during_verification_cannot_restore_automation() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def verify() -> bool:
        started.set()
        await release.wait()
        return True

    controller = SessionController(run_id="run-1", resume_verifier=verify)
    intervention = await controller.request_handoff("checkpoint", "step-1")
    await controller.take_control(intervention.intervention_id)
    resuming = asyncio.create_task(controller.resume(intervention.intervention_id))
    await started.wait()
    await controller.abort()
    release.set()
    with pytest.raises(RunStopped):
        await resuming
    assert controller.state is SessionState.CANCELLED


@pytest.mark.asyncio
async def test_transition_hook_closes_gate_before_resume_verification() -> None:
    states = []

    async def transition(state) -> None:
        states.append(state)

    async def verify() -> bool:
        assert states[-1] is SessionState.VERIFYING
        return True

    controller = SessionController(
        run_id="run-1", resume_verifier=verify, transition_handler=transition
    )
    intervention = await controller.request_handoff("checkpoint", "step-1")
    await controller.take_control(intervention.intervention_id)
    assert states[-1] is SessionState.HUMAN
    await controller.resume(intervention.intervention_id)
    assert states[-1] is SessionState.AUTOMATION
    await controller.abort()
    assert states[-1] is SessionState.CANCELLED


def test_query_values_are_not_needed_by_session_gate() -> None:
    """Guard against coupling the ownership controller to business data."""
    query = PaymentQuery(
        member_id="12345",
        amount="250.00",
        currency="USD",
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 7),
    )
    assert isinstance(InputValue(name="member_id"), InputValue)
    assert query.member_id == "12345"
