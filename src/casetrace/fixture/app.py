from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .data import MEMBERS, PAGE_SIZE, TRANSACTIONS, Transaction
from .scenarios import SCENARIOS, ScenarioState

TEMPLATE_ROOT = Path(__file__).parents[1] / "templates"
DEMO_USERNAME = "demo-operator"
DEMO_PASSWORD = "fixture-passphrase"
SLOW_ONCE_SECONDS = 0.25


def _account_exists(state: ScenarioState, member_id: str, account_id: str) -> bool:
    accounts = state.accounts_for(member_id, MEMBERS.get(member_id, ()))
    return any(account.account_id == account_id for account in accounts)


def _matches_filters(row: Transaction, filters: dict[str, str]) -> bool:
    return (
        (not filters["date_from"] or row.transaction_date >= filters["date_from"])
        and (not filters["date_to"] or row.transaction_date <= filters["date_to"])
        and (not filters["amount"] or row.amount == filters["amount"])
        and (not filters["currency"] or row.currency == filters["currency"])
        and (not filters["direction"] or row.direction == filters["direction"])
        and (not filters["reference"] or row.reference == filters["reference"])
    )


def create_fixture_app(scenario: str) -> FastAPI:
    state = ScenarioState(scenario)
    app = FastAPI(
        title="CaseTrace synthetic bank fixture",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=TEMPLATE_ROOT)

    def render(
        request: Request,
        name: str,
        context: dict[str, object] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=f"bank/{name}",
            context={"scenario": state.name, **(context or {})},
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    async def shell(request: Request) -> HTMLResponse:
        response = render(request, "shell.html")
        if request.cookies.get("fixture_session") is None:
            response.set_cookie("fixture_session", "active", httponly=True, samesite="strict")
        return response

    @app.get("/bank/members", response_class=HTMLResponse)
    async def member_search(request: Request) -> HTMLResponse:
        status = request.cookies.get("fixture_session")
        if status not in {"active", "reauthenticated"} or (
            state.expiration_triggered and status != "reauthenticated"
        ):
            return render(request, "login.html", {"message": "Session expired"}, status_code=401)
        return render(request, "members.html", {"mode": "search", "signed_in": True})

    @app.post("/bank/members/search")
    async def search_member(request: Request, member_id: str = Form()) -> HTMLResponse:
        normalized = member_id.strip()
        if len(normalized) != 5 or not normalized.isdigit():
            return render(
                request,
                "members.html",
                {
                    "mode": "search",
                    "entered_member_id": normalized,
                    "message": "Enter a five-digit member ID.",
                },
                status_code=422,
            )
        return RedirectResponse(f"/bank/members/{normalized}", status_code=303)

    @app.get("/bank/members/{member_id}", response_class=HTMLResponse)
    async def member_summary(request: Request, member_id: str) -> HTMLResponse:
        base_accounts = MEMBERS.get(member_id)
        if base_accounts is None:
            return render(
                request,
                "members.html",
                {
                    "mode": "missing",
                    "requested_member_id": member_id,
                    "message": "Member not found",
                },
                status_code=404,
            )
        accounts = state.accounts_for(member_id, base_accounts)
        return render(
            request,
            "members.html",
            {
                "mode": "summary",
                "requested_member_id": member_id,
                "displayed_member_id": state.displayed_member_id(member_id),
                "accounts": accounts,
                "transfer_attempts": state.transfer_attempts,
                "stale_identity": state.name == "stale-member",
            },
        )

    @app.post("/bank/members/{member_id}/accounts/{account_id}/transfer")
    async def synthetic_transfer(member_id: str, account_id: str) -> RedirectResponse:
        state.transfer_attempts += 1
        return RedirectResponse(
            f"/bank/members/{member_id}?transfer_account={account_id}",
            status_code=303,
        )

    @app.get(
        "/bank/members/{member_id}/accounts/{account_id}/activity",
        response_class=HTMLResponse,
    )
    async def activity(
        request: Request,
        member_id: str,
        account_id: str,
        source: str = "history",
        page: int = 1,
        date_from: str = "",
        date_to: str = "",
        amount: str = "",
        currency: str = "",
        direction: str = "",
        reference: str = "",
    ) -> HTMLResponse:
        if not _account_exists(state, member_id, account_id):
            return render(
                request,
                "activity.html",
                {"condition": "Account not found"},
                status_code=404,
            )
        if source not in {"history", "pending"}:
            return render(
                request,
                "activity.html",
                {"condition": "Activity source not supported"},
                status_code=422,
            )
        if state.needs_login(request.cookies.get("fixture_session")):
            return render(
                request,
                "login.html",
                {"message": "Session expired", "username": DEMO_USERNAME},
                status_code=401,
            )
        if state.name == "permission-denied":
            return render(
                request,
                "activity.html",
                {"condition": "Permission denied", "condition_name": state.name},
                status_code=403,
            )
        if state.name == "timeout":
            return render(
                request,
                "activity.html",
                {
                    "condition": "Timeout scenario: activity did not reach a normal checkpoint.",
                    "condition_name": state.name,
                },
            )
        if state.name == "unknown-dialog":
            return render(
                request,
                "activity.html",
                {
                    "condition": "Unsupported system dialog",
                    "condition_name": state.name,
                    "unknown_dialog": True,
                },
            )

        slowed = state.consume_slowdown()
        if slowed:
            await asyncio.sleep(SLOW_ONCE_SECONDS)

        source_rows = [
            transaction
            for transaction in state.transactions_for(TRANSACTIONS)
            if transaction.member_id == member_id
            and transaction.account_id == account_id
            and transaction.source == source
        ]
        page_count = max(1, math.ceil(len(source_rows) / PAGE_SIZE))
        safe_page = min(max(page, 1), page_count)
        page_rows = source_rows[(safe_page - 1) * PAGE_SIZE : safe_page * PAGE_SIZE]
        filters = {
            "date_from": date_from,
            "date_to": date_to,
            "amount": amount,
            "currency": currency,
            "direction": direction,
            "reference": reference,
        }
        rows = [row for row in page_rows if _matches_filters(row, filters)]
        base_path = f"/bank/members/{member_id}/accounts/{account_id}/activity"
        query = {"source": source, **{key: value for key, value in filters.items() if value}}

        return render(
            request,
            "activity.html",
            {
                "member_id": member_id,
                "account_id": account_id,
                "source": source,
                "rows": rows,
                "filters": filters,
                "filters_applied": any(filters.values()),
                "page": safe_page,
                "page_count": page_count,
                "detail_query": urlencode(query | {"page": safe_page}),
                "previous_url": (
                    f"{base_path}?{urlencode(query | {'page': safe_page - 1})}"
                    if safe_page > 1
                    else None
                ),
                "next_url": (
                    f"{base_path}?{urlencode(query | {'page': safe_page + 1})}"
                    if safe_page < page_count
                    else None
                ),
                "slowed": slowed,
            },
        )

    @app.get(
        "/bank/members/{member_id}/accounts/{account_id}/transactions/{reference}",
        response_class=HTMLResponse,
    )
    async def transaction_detail(
        request: Request,
        member_id: str,
        account_id: str,
        source: str,
        reference: str,
    ) -> HTMLResponse:
        transaction = next(
            (
                row
                for row in state.transactions_for(TRANSACTIONS)
                if row.member_id == member_id
                and row.account_id == account_id
                and row.source == source
                and row.reference == reference
            ),
            None,
        )
        if transaction is None:
            return render(
                request,
                "detail.html",
                {"message": "Transaction not found"},
                status_code=404,
            )
        return_query = {
            key: value
            for key, value in request.query_params.items()
            if key
            in {
                "source",
                "page",
                "date_from",
                "date_to",
                "amount",
                "currency",
                "direction",
                "reference",
            }
        }
        return render(
            request,
            "detail.html",
            {"transaction": transaction, "return_query": urlencode(return_query)},
        )

    @app.get("/bank/login", response_class=HTMLResponse)
    async def login(request: Request) -> HTMLResponse:
        return render(request, "login.html", {"username": DEMO_USERNAME})

    @app.post("/bank/login")
    async def authenticate(
        request: Request,
        username: str = Form(),
        password: str = Form(),
    ) -> HTMLResponse:
        if username != DEMO_USERNAME or password != DEMO_PASSWORD:
            return render(
                request,
                "login.html",
                {
                    "username": username,
                    "message": "Sign-in failed",
                },
                status_code=401,
            )
        response = RedirectResponse("/bank/members", status_code=303)
        response.set_cookie("fixture_session", "reauthenticated", httponly=True, samesite="strict")
        return response

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the synthetic CaseTrace bank")
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    run_fixture(args.scenario, args.port)


def run_fixture(scenario: str = "normal", port: int = 8000) -> None:
    uvicorn.run(
        create_fixture_app(scenario),
        host="127.0.0.1",
        port=port,
        access_log=False,
    )


if __name__ == "__main__":
    main()


def _account_exists(state: ScenarioState, member_id: str, account_id: str) -> bool:
    accounts = state.accounts_for(member_id, MEMBERS.get(member_id, ()))
    return any(account.account_id == account_id for account in accounts)


def _matches_filters(transaction: Transaction, filters: dict[str, str]) -> bool:
    return (
        (not filters["date_from"] or transaction.transaction_date >= filters["date_from"])
        and (not filters["date_to"] or transaction.transaction_date <= filters["date_to"])
        and (not filters["amount"] or transaction.amount == filters["amount"])
        and (not filters["currency"] or transaction.currency == filters["currency"])
        and (not filters["direction"] or transaction.direction == filters["direction"])
        and (not filters["reference"] or transaction.reference == filters["reference"])
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CaseTrace bank fixture")
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        create_fixture_app(args.scenario),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
