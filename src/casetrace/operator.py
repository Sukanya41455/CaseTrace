"""Loopback-only operator controls for a live CaseTrace session."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from html import escape
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .session import SessionController, SessionState


@dataclass(frozen=True, slots=True)
class OperatorDisplay:
    capability_id: str
    goal: str
    step_id: str | None
    stop_reason: str


def create_operator_app(
    session: SessionController,
    display: OperatorDisplay,
    *,
    origin: str = "http://127.0.0.1:8001",
) -> FastAPI:
    """Create controls bound to one controller and one exact loopback origin."""

    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme != "http"
        or parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise ValueError("operator origin must be an HTTP loopback origin")
    expected_origin = origin.rstrip("/")
    expected_host = parsed_origin.netloc.lower()

    app = FastAPI(title="CaseTrace operator", docs_url=None, redoc_url=None)
    app.state.csrf_token = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def require_operator_host(request: Request, call_next):
        if request.headers.get("host", "").lower() != expected_host:
            return PlainTextResponse("forbidden", status_code=403)
        return await call_next(request)

    def authorize_mutation(
        request: Request,
        csrf_token: str,
        intervention_id: str,
        generation: int,
    ) -> None:
        if request.headers.get("origin", "").rstrip("/") != expected_origin:
            raise HTTPException(status_code=403, detail="forbidden")
        if not secrets.compare_digest(csrf_token, app.state.csrf_token):
            raise HTTPException(status_code=403, detail="forbidden")
        current = session.intervention
        if (
            current is None
            or current.intervention_id != intervention_id
            or current.ownership_generation != generation
            or session.ownership_generation != generation
        ):
            raise HTTPException(status_code=409, detail="stale intervention")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        intervention = session.intervention
        intervention_id = intervention.intervention_id if intervention else ""
        generation = intervention.ownership_generation if intervention else -1
        step_id = intervention.step_id if intervention else display.step_id
        reason = intervention.reason if intervention else display.stop_reason
        take_disabled = session.state is not SessionState.PAUSING
        resume_disabled = session.state is not SessionState.HUMAN
        abort_disabled = session.state not in {
            SessionState.PAUSING,
            SessionState.HUMAN,
            SessionState.VERIFYING,
        }

        def form(action: str, label: str, disabled: bool) -> str:
            disabled_attr = " disabled" if disabled else ""
            return (
                f'<form method="post" action="/{action}">'
                f'<input type="hidden" name="csrf_token" value="{escape(app.state.csrf_token)}">'
                f'<input type="hidden" name="intervention_id" value="{escape(intervention_id)}">'
                f'<input type="hidden" name="generation" value="{generation}">'
                f'<button type="submit"{disabled_attr}>{escape(label)}</button>'
                "</form>"
            )

        refresh = (
            "<script>setTimeout(() => location.reload(), 1000);</script>"
            if session.state in {SessionState.AUTOMATION, SessionState.PAUSING}
            else ""
        )
        body = (
            "<!doctype html><html><head><title>CaseTrace operator</title></head><body>"
            "<h1>CaseTrace operator</h1>"
            f"<dl><dt>Capability</dt><dd>{escape(display.capability_id)}</dd>"
            f"<dt>Goal</dt><dd>{escape(display.goal)}</dd>"
            f"<dt>Step</dt><dd>{escape(step_id or 'none')}</dd>"
            f"<dt>Stop reason</dt><dd>{escape(reason)}</dd>"
            f"<dt>Ownership</dt><dd>{escape(session.state.value)}</dd>"
            f"<dt>Run</dt><dd>{escape(session.run_id)}</dd>"
            f"<dt>Session</dt><dd>{escape(session.browser_id)}</dd></dl>"
            + form("take-control", "Take control", take_disabled)
            + form("resume", "Resume", resume_disabled)
            + form("abort", "Abort", abort_disabled)
            + refresh
            + "</body></html>"
        )
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})

    @app.post("/take-control")
    async def take_control(
        request: Request,
        csrf_token: Annotated[str, Form()],
        intervention_id: Annotated[str, Form()],
        generation: Annotated[int, Form()],
    ) -> RedirectResponse:
        authorize_mutation(request, csrf_token, intervention_id, generation)
        try:
            await session.take_control(intervention_id)
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail="transition rejected") from error
        return RedirectResponse("/", status_code=303)

    @app.post("/resume")
    async def resume(
        request: Request,
        csrf_token: Annotated[str, Form()],
        intervention_id: Annotated[str, Form()],
        generation: Annotated[int, Form()],
    ) -> RedirectResponse:
        authorize_mutation(request, csrf_token, intervention_id, generation)
        try:
            await session.resume(intervention_id)
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail="transition rejected") from error
        return RedirectResponse("/", status_code=303)

    @app.post("/abort")
    async def abort(
        request: Request,
        csrf_token: Annotated[str, Form()],
        intervention_id: Annotated[str, Form()],
        generation: Annotated[int, Form()],
    ) -> RedirectResponse:
        authorize_mutation(request, csrf_token, intervention_id, generation)
        await session.abort()
        return RedirectResponse("/", status_code=303)

    return app
