import asyncio

import pytest

from casetrace.contracts import RunStopped
from casetrace.session import SessionController, SessionState


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_state", [SessionState.HUMAN, SessionState.AUTOMATION])
async def test_transition_failure_revokes_ownership_and_closes_gate(failed_state):
    gate = False

    async def transition(state):
        nonlocal gate
        gate = state is SessionState.HUMAN
        if state is failed_state:
            raise RuntimeError("private callback exception")

    async def verify():
        return True

    controller = SessionController("test", transition_handler=transition, resume_verifier=verify)
    intervention = await controller.request_handoff("checkpoint", "root")
    with pytest.raises(RunStopped) as stopped:
        await controller.take_control(intervention.id)
        await controller.resume(intervention.id)
    assert "private callback exception" not in str(stopped.value)
    assert controller.state is SessionState.CANCELLED
    assert controller.owner == "system"
    assert not gate
    with pytest.raises(RunStopped):
        await controller.execute(lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_transition_callback_does_not_hold_state_lock():
    async def transition(state):
        async with controller._state_lock:
            assert controller.state is state

    controller = SessionController("test", transition_handler=transition)
    await asyncio.wait_for(controller.request_handoff("checkpoint", "root"), timeout=1)


@pytest.mark.asyncio
async def test_human_gate_opens_only_after_inflight_drains():
    started, release = asyncio.Event(), asyncio.Event()
    states = []

    async def transition(state):
        states.append(state)

    async def operation():
        started.set()
        await release.wait()

    controller = SessionController("test", transition_handler=transition)
    running = asyncio.create_task(controller.execute(operation))
    await started.wait()
    intervention = await controller.request_handoff("checkpoint", "root")
    takeover = asyncio.create_task(controller.take_control(intervention.id))
    await asyncio.sleep(0)
    assert states == [SessionState.PAUSING]
    release.set()
    with pytest.raises(RunStopped):
        await running
    await takeover
    assert states == [SessionState.PAUSING, SessionState.HUMAN]
