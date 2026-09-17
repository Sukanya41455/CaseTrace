from __future__ import annotations

import re

import httpx
import pytest

from casetrace.operator import OperatorDisplay, create_operator_app
from casetrace.session import SessionController, SessionState

ORIGIN = "http://127.0.0.1:8001"


async def _client(
    session: SessionController,
) -> tuple[httpx.AsyncClient, object]:
    app = create_operator_app(
        session,
        OperatorDisplay(
            capability_id="trace_incoming_payment",
            goal="Find the requested incoming payment",
            step_id="search-member",
            stop_reason="Authentication required",
        ),
        origin=ORIGIN,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
    return client, app


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.mark.asyncio
async def test_operator_page_shows_sanitized_run_state_and_current_controls():
    session = SessionController(run_id="run-operator")
    intervention = await session.request_handoff("authentication required", "search-member")
    client, _ = await _client(session)
    async with client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "trace_incoming_payment" in response.text
    assert "run-operator" in response.text
    assert "PAUSING" in response.text
    assert "authentication required" in response.text
    assert intervention.intervention_id in response.text
    assert "csrf_token" in response.text
    assert "setTimeout" not in response.text


@pytest.mark.asyncio
async def test_cross_origin_request_cannot_take_control():
    session = SessionController(run_id="run-operator")
    intervention = await session.request_handoff("authentication required", "search-member")
    client, app = await _client(session)
    async with client:
        response = await client.post(
            "/take-control",
            headers={"origin": "http://attacker.invalid"},
            data={
                "csrf_token": app.state.csrf_token,
                "intervention_id": intervention.intervention_id,
                "generation": intervention.ownership_generation,
            },
        )

    assert response.status_code == 403
    assert session.state is SessionState.PAUSING


@pytest.mark.asyncio
async def test_wrong_csrf_token_cannot_change_session():
    session = SessionController(run_id="run-operator")
    intervention = await session.request_handoff("authentication required", "search-member")
    client, _ = await _client(session)
    async with client:
        response = await client.post(
            "/take-control",
            headers={"origin": ORIGIN},
            data={
                "csrf_token": "wrong-token",
                "intervention_id": intervention.intervention_id,
                "generation": intervention.ownership_generation,
            },
        )

    assert response.status_code == 403
    assert session.state is SessionState.PAUSING


@pytest.mark.asyncio
async def test_take_control_and_verified_resume_use_current_intervention_generation():
    async def verified() -> bool:
        return True

    session = SessionController(run_id="run-operator", resume_verifier=verified)
    intervention = await session.request_handoff("authentication required", "search-member")
    client, app = await _client(session)
    headers = {"origin": ORIGIN}
    async with client:
        take = await client.post(
            "/take-control",
            headers=headers,
            data={
                "csrf_token": app.state.csrf_token,
                "intervention_id": intervention.intervention_id,
                "generation": intervention.ownership_generation,
            },
        )
        assert take.status_code == 303
        assert session.state is SessionState.HUMAN

        stale = await client.post(
            "/resume",
            headers=headers,
            data={
                "csrf_token": app.state.csrf_token,
                "intervention_id": intervention.intervention_id,
                "generation": intervention.ownership_generation,
            },
        )
        assert stale.status_code == 409
        assert session.state is SessionState.HUMAN

        current = session.intervention
        assert current is not None
        resume = await client.post(
            "/resume",
            headers=headers,
            data={
                "csrf_token": app.state.csrf_token,
                "intervention_id": current.intervention_id,
                "generation": current.ownership_generation,
            },
        )

    assert resume.status_code == 303
    assert session.state is SessionState.AUTOMATION


@pytest.mark.asyncio
async def test_abort_uses_controller_and_rejects_stale_generation():
    session = SessionController(run_id="run-operator")
    intervention = await session.request_handoff("authentication required", "search-member")
    client, app = await _client(session)
    headers = {"origin": ORIGIN}
    async with client:
        stale = await client.post(
            "/abort",
            headers=headers,
            data={
                "csrf_token": app.state.csrf_token,
                "intervention_id": intervention.intervention_id,
                "generation": intervention.ownership_generation + 1,
            },
        )
        assert stale.status_code == 409

        aborted = await client.post(
            "/abort",
            headers=headers,
            data={
                "csrf_token": app.state.csrf_token,
                "intervention_id": intervention.intervention_id,
                "generation": intervention.ownership_generation,
            },
        )

    assert aborted.status_code == 303
    assert session.state is SessionState.CANCELLED
