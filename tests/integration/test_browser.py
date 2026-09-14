from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from casetrace.browser import BrowserSurface
from casetrace.contracts import (
    AccessibleRoleStrategy,
    AdjacentControlStrategy,
    ClickStep,
    FailureCode,
    FillStep,
    InputValue,
    LiteralValue,
    NavigateStep,
    PaymentQuery,
    PolicyConfig,
    Provenance,
    ReadStep,
    RunStopped,
    TableRelationStrategy,
    TargetSpec,
    VariableValue,
    VisibleTextStrategy,
)
from casetrace.evidence import EvidenceWriter
from casetrace.policy import Policy
from casetrace.session import SessionController, SessionState
from tests.harness import FixtureServer


def _provenance() -> Provenance:
    return Provenance(kind="authored", validation_scenario="normal")


def _policy(origin: str) -> Policy:
    raw = json.loads(Path("config/base.json").read_text(encoding="utf-8"))
    return Policy(PolicyConfig.model_validate(raw["policy"]), tenant_origin=origin)


def _targets() -> list[TargetSpec]:
    frame = ["Bank workspace"]
    return [
        TargetSpec(
            target_id="member-id",
            surface_kind="web",
            frame_path=frame,
            strategies=[AccessibleRoleStrategy(role="textbox", name="Member ID")],
        ),
        TargetSpec(
            target_id="search",
            surface_kind="web",
            frame_path=frame,
            strategies=[AccessibleRoleStrategy(role="button", name="Search")],
        ),
        TargetSpec(
            target_id="transfer",
            surface_kind="web",
            frame_path=frame,
            container="Deposit accounts",
            strategies=[
                TableRelationStrategy(
                    header="Account ID",
                    row_value=LiteralValue(value="SYNTH-DDA-A1"),
                    control_role="button",
                )
            ],
        ),
        TargetSpec(
            target_id="counter",
            surface_kind="web",
            frame_path=frame,
            strategies=[VisibleTextStrategy(text="Transfer attempts: 0", exact=True)],
        ),
    ]


def _query() -> PaymentQuery:
    return PaymentQuery.model_validate(
        {
            "member_id": "12345",
            "amount": "250.00",
            "currency": "USD",
            "date_from": "2026-09-01",
            "date_to": "2026-09-07",
        }
    )


def _navigate() -> NavigateStep:
    return NavigateStep(
        step_id="navigate",
        url=VariableValue(name="entry_url"),
        checks=[],
        checkpoint=None,
        provenance=_provenance(),
    )


@pytest.mark.asyncio
async def test_denied_control_is_never_activated() -> None:
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), _targets(), headless=True)
        try:
            query = _query()
            surface.bind_query(query)
            await surface.act(_navigate(), query, {"entry_url": fixture.url})
            await surface.act(
                FillStep(
                    step_id="fill-member",
                    target_id="member-id",
                    value=InputValue(name="member_id"),
                    checks=[],
                    checkpoint=None,
                    provenance=_provenance(),
                ),
                query,
                {},
            )
            await surface.act(
                ClickStep(
                    step_id="search-member",
                    target_id="search",
                    checks=[],
                    checkpoint=None,
                    provenance=_provenance(),
                ),
                query,
                {},
            )
            assert await surface.read_text("counter", query, {}) == "Transfer attempts: 0"
            observation = await surface.observe()
            tables = [item for item in observation.controls if item.role == "table"]
            assert any(item.label == "Deposit accounts" for item in tables)

            with pytest.raises(RunStopped) as failure:
                await surface.act(
                    ClickStep(
                        step_id="transfer-money",
                        target_id="transfer",
                        checks=[],
                        checkpoint=None,
                        provenance=_provenance(),
                    ),
                    query,
                    {},
                )

            assert failure.value.code is FailureCode.POLICY_DENIED
            assert await surface.read_text("counter", query, {}) == "Transfer attempts: 0"
        finally:
            await surface.close()


class _TrapHandler(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:  # noqa: N802
        type(self).hits += 1
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


@contextmanager
def _trap_server():
    _TrapHandler.hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TrapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@contextmanager
def _delivery_server(trap: str, mode: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if mode in {"redirect", "redirect-chain"}:
                self.send_response(302)
                destination = (
                    "/bank/members" if mode == "redirect-chain" and self.path == "/" else trap
                )
                self.send_header("Location", destination)
                self.end_headers()
                return
            html = {
                "frame": f'<iframe src="{trap}"></iframe>',
                "image": f'<img src="{trap}/image">',
                "popup": f'<script>window.open("{trap}")</script>',
            }[mode]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["redirect", "redirect-chain", "frame", "image", "popup"])
async def test_denied_subrequests_never_reach_other_origin(mode: str) -> None:
    with _trap_server() as trap, _delivery_server(trap, mode) as origin:
        surface = await BrowserSurface.launch(_policy(origin), [], headless=True)
        try:
            if mode == "popup":
                async with surface._context.expect_page():
                    try:
                        await surface.act(_navigate(), _query(), {"entry_url": origin})
                    except RunStopped as stopped:
                        assert stopped.code is FailureCode.POLICY_DENIED
                with pytest.raises(RunStopped) as failure:
                    await surface.observe()
            else:
                with pytest.raises(RunStopped) as failure:
                    await surface.act(_navigate(), _query(), {"entry_url": origin})
            assert failure.value.code is FailureCode.POLICY_DENIED
            assert _TrapHandler.hits == 0
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_denied_origin_is_blocked_before_request_reaches_server() -> None:
    with FixtureServer() as fixture, _trap_server() as trap:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            query = _query()
            with pytest.raises(RunStopped) as failure:
                await surface.act(
                    NavigateStep(
                        step_id="escape",
                        url=LiteralValue(value=trap),
                        checks=[],
                        checkpoint=None,
                        provenance=_provenance(),
                    ),
                    query,
                    {},
                )
            assert failure.value.code is FailureCode.POLICY_DENIED
            assert _TrapHandler.hits == 0
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_observation_and_capture_are_sanitized(tmp_path) -> None:
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), _targets(), headless=True)
        try:
            query = _query()
            surface.bind_query(query)
            await surface.act(_navigate(), query, {"entry_url": fixture.url})
            observation = await surface.observe()
            fingerprint = observation.state["page_fingerprint"]
            assert len(fingerprint) == 64
            assert (await surface.observe()).state["page_fingerprint"] == fingerprint
            assert observation.vendor == "Northstar Synthetic Bank"
            assert observation.app_version == "2026.09"
            assert "frames" in observation.surface_features
            assert all(query.member_id not in control.label for control in observation.controls)

            writer = EvidenceWriter(tmp_path, "browser-run", sensitive_values=[query.member_id])
            captured = await writer.capture(surface)
            snapshot = json.loads(Path(captured["snapshot_path"]).read_text(encoding="utf-8"))
            assert query.member_id not in json.dumps(snapshot)
            assert snapshot["vendor"] == "Northstar Synthetic Bank"
            assert "screenshot_path" in captured
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_observation_reports_automated_fill_without_exposing_value() -> None:
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            query = _query()
            surface.bind_query(query)
            await surface.act(_navigate(), query, {"entry_url": fixture.url})
            before = await surface.observe()
            member = next(control for control in before.controls if control.label == "Member ID")
            assert member.filled_by_automation is False

            await surface.act(
                FillStep(
                    step_id="fill-member",
                    target_id=member.target_handle,
                    value=InputValue(name="member_id"),
                    checks=[],
                    checkpoint=None,
                    provenance=_provenance(),
                ),
                query,
                {},
            )
            after = await surface.observe()
            member = next(control for control in after.controls if control.label == "Member ID")
            assert member.filled_by_automation is True
            assert query.member_id not in after.model_dump_json()
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_observation_reports_restored_value_without_exposing_it() -> None:
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            query = _query()
            surface.bind_query(query)
            await surface.act(
                _navigate(),
                query,
                {
                    "entry_url": (
                        fixture.url
                        + "/bank/members/12345/accounts/SYNTH-DDA-A1/activity"
                        + "?source=history&amount=250.00"
                    )
                },
            )
            observation = await surface.observe()
            amount = next(control for control in observation.controls if control.label == "Amount")

            assert amount.has_value is True
            assert amount.filled_by_automation is False
            assert query.amount not in observation.model_dump_json()
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_detail_fields_are_read_from_visible_table() -> None:
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            query = _query()
            surface.bind_query(query)
            await surface.act(
                _navigate(),
                query,
                {
                    "entry_url": fixture.url
                    + "/bank/members/12345/accounts/SYNTH-DDA-A1/transactions/"
                    "REF-POST-250?source=history"
                },
            )
            observation = await surface.observe()
            target = next(item for item in observation.controls if item.role == "table")
            result = await surface.read(
                ReadStep(
                    step_id="detail",
                    target_id=target.target_handle,
                    parser="fields",
                    read_role="payment_detail",
                    store_as="payment",
                    checks=[],
                    checkpoint=None,
                    provenance=_provenance(),
                ),
                query,
                {},
            )
            assert result["member_id"] == "12345"
            assert result["reference"] == "REF-POST-250"
            assert result["source"] == "history"
            assert result["observation_id"] == observation.observation_id
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_adjacent_date_and_select_controls_support_reviewed_fills() -> None:
    with FixtureServer() as fixture:
        targets = [
            TargetSpec(
                target_id="start",
                surface_kind="web",
                strategies=[AdjacentControlStrategy(label="Start date", control_role="textbox")],
            ),
            TargetSpec(
                target_id="currency",
                surface_kind="web",
                strategies=[AccessibleRoleStrategy(role="combobox", name="Currency")],
            ),
        ]
        surface = await BrowserSurface.launch(_policy(fixture.url), targets, headless=True)
        try:
            query = _query()
            await surface.act(
                _navigate(),
                query,
                {
                    "entry_url": fixture.url
                    + "/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history"
                },
            )
            for target_id, value in [("start", "2026-09-01"), ("currency", "USD")]:
                await surface.act(
                    FillStep(
                        step_id="fill-" + target_id,
                        target_id=target_id,
                        value=LiteralValue(value=value),
                        checks=[],
                        checkpoint=None,
                        provenance=_provenance(),
                    ),
                    query,
                    {},
                )
            assert (
                await surface._page.locator('input[name="date_from"]').input_value() == "2026-09-01"
            )
            assert (
                await surface._page.get_by_role("combobox", name="Currency").input_value() == "USD"
            )
        finally:
            await surface.close()


def _resume_identity(surface):
    return {
        "browser_id": surface.browser_id,
        "context_id": surface.context_id,
        "page_id": surface.page_id,
        "vendor": "Northstar Synthetic Bank",
        "app_version": "2026.09",
    }


@pytest.mark.asyncio
async def test_human_gate_survives_frame_navigation_and_closes_for_keyboard_input():
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            await surface.act(_navigate(), _query(), {"entry_url": fixture.url})
            await surface.set_human_control(True)
            frame = surface._page.frame(name="content")
            await frame.get_by_role("textbox", name="Member ID").fill("12345")
            await frame.get_by_role("button", name="Search").click()
            await frame.get_by_role("heading", name="Member 12345", exact=True).wait_for()
            await surface._page.get_by_role("link", name="Member search", exact=True).click()
            field = frame.get_by_role("textbox", name="Member ID")
            await field.wait_for()
            await field.press_sequentially("67890")
            assert await field.input_value() == "67890"
            await surface.set_human_control(False)
            await field.press_sequentially("12345")
            assert await field.input_value() == "67890"
            await frame.evaluate("() => __casetraceSetOwnership({enabled: true, generation: 0})")
            await field.press_sequentially("12345")
            assert await field.input_value() == "67890"
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_resume_checks_original_live_ids_supported_identity_and_known_search():
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), _targets(), headless=True)
        try:
            await surface.act(_navigate(), _query(), {"entry_url": fixture.url})
            identity = _resume_identity(surface)
            assert (await surface.verify_resume(**identity))[0]
            for key in identity:
                assert not (await surface.verify_resume(**(identity | {key: "changed"})))[0]
            await surface.set_human_control(True)
            frame = surface._page.frame(name="content")
            await frame.get_by_role("textbox", name="Member ID").fill("12345")
            await frame.get_by_role("button", name="Search").click()
            await frame.get_by_role("heading", name="Member 12345", exact=True).wait_for()
            await surface.set_human_control(False)
            assert not (await surface.verify_resume(**identity))[0]
            await surface._page.close()
            assert not (await surface.verify_resume(**identity))[0]
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_resume_rejects_visible_dialog_and_absent_authentication_marker():
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            await surface.act(_navigate(), _query(), {"entry_url": fixture.url})
            identity = _resume_identity(surface)
            frame = surface._page.frame(name="content")
            await frame.locator("body").evaluate(
                "el => el.insertAdjacentHTML('beforeend', '<div role=dialog>Unknown</div>')"
            )
            assert not (await surface.verify_resume(**identity))[0]
            await frame.get_by_role("dialog").evaluate("el => el.remove()")
            marker = frame.get_by_text("Signed in as: demo-operator", exact=True)
            await marker.evaluate("el => el.remove()")
            assert not (await surface.verify_resume(**identity))[0]
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_resume_rejects_native_dialog_until_dismissed():
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            await surface.act(_navigate(), _query(), {"entry_url": fixture.url})
            async with surface._page.expect_event("dialog") as opened:
                await surface._page.evaluate("() => { setTimeout(() => alert('Unknown'), 0); }")
            dialog = await opened.value
            verified = await asyncio.wait_for(
                surface.verify_resume(**_resume_identity(surface)), timeout=1
            )
            assert not verified[0]
            await dialog.dismiss()
            assert (await surface.verify_resume(**_resume_identity(surface)))[0]
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_blocked_ownership_update_closes_browser_before_timeout_escapes():
    with FixtureServer() as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), [], headless=True)
        try:
            await surface.act(_navigate(), _query(), {"entry_url": fixture.url})
            await surface.set_human_control(True)
            async with surface._page.expect_event("dialog"):
                await surface._page.evaluate("() => { setTimeout(() => alert('Unknown'), 0); }")
            surface.action_timeout_ms = 20
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(surface.set_human_control(False), timeout=1)
            assert surface._page.is_closed()
        finally:
            await surface.close()


@pytest.mark.asyncio
async def test_simulated_operator_reauthenticates_original_session_before_resume():
    """Authored test-operator evidence; it does not replace the real human demonstration."""
    with FixtureServer("expire-once") as fixture:
        surface = await BrowserSurface.launch(_policy(fixture.url), _targets(), headless=True)
        try:
            identity = _resume_identity(surface)

            async def transition(state):
                await surface.set_human_control(state is SessionState.HUMAN)

            async def verify():
                return await surface.verify_resume(**identity)

            session = SessionController(
                "simulated-operator",
                browser_id=surface.browser_id,
                context_id=surface.context_id,
                page_id=surface.page_id,
                transition_handler=transition,
                resume_verifier=verify,
            )
            await session.execute(
                lambda: surface.act(_navigate(), _query(), {"entry_url": fixture.url})
            )
            frame = surface._page.frame(name="content")
            # Authored test navigation triggers the fixture's one-time expiration.
            await frame.goto(fixture.url + "/bank/members/12345/accounts/SYNTH-DDA-A1/activity")
            assert (await surface.observe()).state["screen"] == "authentication"
            intervention = await session.request_handoff("authentication required", "search")
            await session.take_control(intervention.id)
            with pytest.raises(RunStopped):
                await session.resume(intervention.id)
            assert session.state is SessionState.HUMAN
            await frame.get_by_role("textbox", name="Username", exact=True).fill("demo-operator")
            await frame.get_by_label("Password", exact=True).fill("fixture-passphrase")
            await frame.get_by_role("button", name="Sign in", exact=True).click()
            await frame.get_by_text("Signed in as: demo-operator", exact=True).wait_for()
            await session.resume(intervention.id)
            assert session.state is SessionState.AUTOMATION
            assert _resume_identity(surface) == identity
            assert session.intervention.state == "resumed"
            events = surface.manual_events()
            assert any(not event["blocked"] for event in events)
            assert "fixture-passphrase" not in json.dumps(events)
        finally:
            await surface.close()
