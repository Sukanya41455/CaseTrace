from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from casetrace.fixture.app import create_fixture_app
from tests.harness import FixtureServer

QUERY_ROOT = Path(__file__).parents[2] / "examples" / "queries"
WINDOW = {"date_from": "2026-09-01", "date_to": "2026-09-07"}


def test_expired_search_requires_login_before_showing_authenticated_identity() -> None:
    with TestClient(create_fixture_app("expire-once")) as client:
        client.get("/")
        assert "Signed in as: demo-operator" in client.get("/bank/members").text
        client.get("/bank/members/12345/accounts/SYNTH-DDA-A1/activity")
        expired = client.get("/bank/members")
        assert expired.status_code == 401
        assert "Signed in as: demo-operator" not in expired.text
        signed_in = client.post(
            "/bank/login",
            data={"username": "demo-operator", "password": "fixture-passphrase"},
        )
        assert signed_in.status_code == 200
        assert "Signed in as: demo-operator" in signed_in.text


def test_example_queries_capture_every_required_case() -> None:
    expected = {
        "posted.json": ("12345", "250.00"),
        "posted-new-member.json": ("54321", "175.50"),
        "pending.json": ("12345", "125.00"),
        "reversed.json": ("12345", "80.00"),
        "ambiguous.json": ("12345", "60.00"),
        "not-found.json": ("12345", "999.00"),
        "member-not-found.json": ("99999", "250.00"),
        "no-accounts.json": ("11111", "250.00"),
        "invalid.json": ("12345", "-1.00"),
    }

    observed = {}
    for name in expected:
        query = json.loads((QUERY_ROOT / name).read_text(encoding="utf-8"))
        observed[name] = (query["member_id"], query["amount"])
        assert query | WINDOW == query
        assert query["currency"] == "USD"
        assert query["reference"] is None

    assert observed == expected


@pytest.mark.asyncio
async def test_fixture_has_real_frame_and_complete_manual_search() -> None:
    with FixtureServer() as server:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(server.url)
            content = page.frame_locator('iframe[name="content"]')
            await content.get_by_role("textbox").fill("12345")
            await content.get_by_role("button", name="Search", exact=True).click()
            await content.get_by_text("Member 12345", exact=True).wait_for()
            assert await content.get_by_text("Deposit accounts", exact=True).count() == 1

            account_row = content.get_by_role("row").filter(has_text="SYNTH-DDA-A1")
            await account_row.get_by_role("link", name="Open", exact=True).click()
            await content.get_by_role("heading", name="Account activity").wait_for()
            await content.get_by_role("textbox", name="Amount").fill("250.00")
            await content.get_by_role("combobox", name="Currency").select_option("USD")
            await content.get_by_role("combobox", name="Direction").select_option("CREDIT")
            await content.locator('input[name="date_from"]').fill("2026-09-01")
            await content.locator('input[name="date_to"]').fill("2026-09-07")
            await content.get_by_role("button", name="Apply filters").click()
            await content.get_by_text("Applied filters", exact=False).wait_for()
            assert await content.get_by_text("No activity on this page").count() == 1
            await content.get_by_role("link", name="Next", exact=True).click()
            match_row = content.get_by_role("row").filter(has_text="REF-POST-250")
            assert await match_row.get_by_text("POSTED", exact=True).count() == 1
            await match_row.get_by_role("link", name="View", exact=True).click()
            await content.get_by_text("Transaction details", exact=True).wait_for()
            detail = content.get_by_role("table", name="Transaction details")
            for label, value in (
                ("Member ID", "12345"),
                ("Account ID", "SYNTH-DDA-A1"),
                ("Transaction reference", "REF-POST-250"),
                ("Displayed status", "POSTED"),
            ):
                row = detail.get_by_role("row").filter(has_text=label)
                assert await row.get_by_text(value, exact=True).count() == 1
            await content.get_by_role("link", name="Back to activity", exact=True).click()
            await content.get_by_text("Page 2 of 2", exact=True).wait_for()
            assert await content.get_by_role("textbox", name="Amount").input_value() == "250.00"
            assert await content.get_by_text("REF-POST-250", exact=True).count() == 1
            await browser.close()


@pytest.mark.asyncio
async def test_expire_once_reauthenticates_in_the_same_browser_session() -> None:
    with FixtureServer("expire-once") as server:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(server.url)
            content = page.frame_locator('iframe[name="content"]')
            await content.get_by_label("Member ID").fill("12345")
            await content.get_by_role("button", name="Search", exact=True).click()
            await content.get_by_text("Member 12345", exact=True).wait_for()
            account_row = content.get_by_role("row").filter(has_text="SYNTH-DDA-A1")
            await account_row.get_by_role("link", name="Open", exact=True).click()
            await content.get_by_role("heading", name="Demo sign in").wait_for()

            await content.get_by_label("Username").fill("demo-operator")
            await content.get_by_label("Password").fill("fixture-passphrase")
            await content.get_by_role("button", name="Sign in", exact=True).click()
            await content.get_by_role("heading", name="Member search").wait_for()

            assert page.context is context
            assert len(context.pages) == 1
            await content.get_by_label("Member ID").fill("12345")
            await content.get_by_role("button", name="Search", exact=True).click()
            await content.get_by_text("Member 12345", exact=True).wait_for()
            account_row = content.get_by_role("row").filter(has_text="SYNTH-DDA-A1")
            await account_row.get_by_role("link", name="Open", exact=True).click()
            await content.get_by_role("heading", name="Account activity").wait_for()
            await browser.close()


def test_member_and_account_relationships_are_visible() -> None:
    with TestClient(create_fixture_app("normal")) as client:
        response = client.post(
            "/bank/members/search",
            data={"member_id": "54321"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert "Member 54321" in response.text
    assert response.text.count("<td>SYNTH-DDA-B") == 2
    assert "Deposit accounts" in response.text


def test_shell_exposes_reviewed_vendor_and_application_version() -> None:
    with TestClient(create_fixture_app("normal")) as client:
        response = client.get("/")
    assert "Vendor: Northstar Synthetic Bank" in response.text
    assert "Application version: 2026.09" in response.text


def test_transfer_attempt_oracle_changes_only_after_control_activation() -> None:
    with TestClient(create_fixture_app("normal")) as client:
        before = client.get("/bank/members/12345")
        activated = client.post(
            "/bank/members/12345/accounts/SYNTH-DDA-A1/transfer",
            follow_redirects=True,
        )
    assert "Transfer attempts: 0" in before.text
    assert "Transfer attempts: 1" in activated.text


def test_fixture_does_not_expose_api_or_scenario_controls() -> None:
    with TestClient(create_fixture_app("normal")) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/scenario").status_code == 404


def test_cli_fixture_command_starts_the_isolated_server() -> None:
    with FixtureServer(launcher="cli") as server:
        with urllib.request.urlopen(server.url, timeout=1) as response:
            html = response.read().decode("utf-8")
    assert response.status == 200
    assert '<iframe name="content"' in html


def test_reviewed_transaction_route_uses_source_query() -> None:
    with TestClient(create_fixture_app("normal")) as client:
        response = client.get(
            "/bank/members/12345/accounts/SYNTH-DDA-A1/transactions/REF-POST-250",
            params={"source": "history"},
        )
    assert response.status_code == 200
    assert "Transaction details" in response.text
    assert "REF-POST-250" in response.text


def test_member_not_found_and_no_accounts_are_distinct() -> None:
    with TestClient(create_fixture_app("normal")) as client:
        missing = client.post(
            "/bank/members/search",
            data={"member_id": "99999"},
            follow_redirects=True,
        )
        empty = client.post(
            "/bank/members/search",
            data={"member_id": "11111"},
            follow_redirects=True,
        )
    assert "Member not found" in missing.text
    assert "Member 11111" in empty.text
    assert "No deposit accounts" in empty.text


def test_expire_once_requires_real_login_and_returns_to_member_search() -> None:
    with TestClient(create_fixture_app("expire-once")) as client:
        client.get("/")
        expired = client.get("/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history")
        rejected = client.post(
            "/bank/login",
            data={"username": "demo-operator", "password": "wrong"},
        )
        resumed = client.post(
            "/bank/login",
            data={"username": "demo-operator", "password": "fixture-passphrase"},
            follow_redirects=False,
        )
        member_search = client.get(resumed.headers["location"])
        activity = client.get("/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history")
    assert "Session expired" in expired.text
    assert "Demo sign in" in expired.text
    assert "Sign-in failed" in rejected.text
    assert resumed.status_code == 303
    assert resumed.headers["location"] == "/bank/members"
    assert "Member search" in member_search.text
    assert "Account activity" in activity.text


@pytest.mark.parametrize(
    ("scenario", "path", "visible_condition"),
    [
        (
            "permission-denied",
            "/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history",
            "Permission denied",
        ),
        (
            "timeout",
            "/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history",
            "Timeout scenario",
        ),
        (
            "unknown-dialog",
            "/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history",
            "Unsupported system dialog",
        ),
        ("stale-member", "/bank/members/12345", "Member 54321"),
        ("search-limit", "/bank/members/12345", "4 deposit accounts"),
        (
            "conflicting-record",
            "/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=pending",
            "REF-POST-250",
        ),
    ],
)
def test_exception_scenarios_render_visible_conditions(
    scenario: str, path: str, visible_condition: str
) -> None:
    with TestClient(create_fixture_app(scenario)) as client:
        response = client.get(path)
    assert visible_condition in response.text


def test_slow_once_delays_only_first_safe_activity_load() -> None:
    with TestClient(create_fixture_app("slow-once")) as client:
        started = time.monotonic()
        first = client.get("/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history")
        first_duration = time.monotonic() - started
        started = time.monotonic()
        second = client.get("/bank/members/12345/accounts/SYNTH-DDA-A1/activity?source=history")
        second_duration = time.monotonic() - started
    assert first_duration >= 0.2
    assert second_duration < 0.2
    assert "Temporary slowdown recovered" in first.text
    assert "Temporary slowdown recovered" not in second.text


def test_unknown_scenario_is_rejected_before_server_start() -> None:
    with pytest.raises(ValueError, match="unsupported fixture scenario"):
        create_fixture_app("surprise")
