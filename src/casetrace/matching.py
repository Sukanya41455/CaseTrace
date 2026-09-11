"""Exact matching over verified, run-local UI records."""

from decimal import Decimal

from casetrace.contracts import (
    BusinessDecision,
    BusinessOutcomeCode,
    CandidateSummary,
    FailureCode,
    FailureDecision,
    MatchDecision,
    PaymentDecision,
    PaymentQuery,
    PaymentRecord,
    SearchCoverage,
)


def classify_payment(
    query: PaymentQuery, records: list[PaymentRecord], coverage: SearchCoverage
) -> PaymentDecision:
    if not coverage.complete:
        return FailureDecision(
            code=FailureCode.CHECKPOINT_FAILED,
            coverage=coverage,
            expected="all accounts, sources, and pages exhausted",
            observed="incomplete search coverage",
        )

    unique: dict[tuple[str, str, str], PaymentRecord] = {}
    for record in records:
        if record.member_id != query.member_id:
            continue
        identity = (record.member_id, record.account_id, record.reference)
        previous = unique.get(identity)
        if previous is not None and _details(previous) != _details(record):
            return FailureDecision(
                code=FailureCode.INCONSISTENT_RECORD,
                coverage=coverage,
                expected="consistent details for each transaction identity",
                observed="conflicting displayed transaction details",
            )
        unique.setdefault(identity, record)

    matches = [
        record
        for record in unique.values()
        if record.direction == "CREDIT"
        and record.currency == query.currency
        and Decimal(record.amount) == Decimal(query.amount)
        and query.date_from <= record.transaction_date <= query.date_to
        and (query.reference is None or record.reference == query.reference)
    ]
    if len(matches) == 1:
        return MatchDecision(records=matches, coverage=coverage)
    if not matches:
        return BusinessDecision(code=BusinessOutcomeCode.NOT_FOUND, coverage=coverage)

    accounts: dict[str, str] = {}
    summaries = []
    for index, record in enumerate(matches, start=1):
        alias = accounts.setdefault(record.account_id, f"account-{len(accounts) + 1}")
        summaries.append(
            CandidateSummary(
                account_alias=alias, reference_alias=f"reference-{index}", status=record.status
            )
        )
    return BusinessDecision(
        code=BusinessOutcomeCode.AMBIGUOUS, coverage=coverage, candidates=summaries
    )


def _details(record: PaymentRecord) -> tuple:
    # Source and observation ID identify sightings, not different transactions.
    return (
        record.direction,
        Decimal(record.amount),
        record.currency,
        record.transaction_date,
        record.status,
    )
