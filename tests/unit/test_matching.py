from datetime import date

import pytest

from casetrace.contracts import PaymentRecord, SearchCoverage
from casetrace.matching import classify_payment


def record(**changes) -> PaymentRecord:
    return PaymentRecord.model_validate(
        {
            "member_id": "12345",
            "account_id": "SYNTH-A1",
            "reference": "REF-ONE",
            "direction": "CREDIT",
            "amount": "250.00",
            "currency": "USD",
            "transaction_date": "2026-09-03",
            "status": "POSTED",
            "source": "history",
            "observation_id": "obs-one",
        }
        | changes
    )


def coverage(**changes) -> SearchCoverage:
    return SearchCoverage(
        **(
            {
                "accounts_complete": True,
                "sources_complete": True,
                "pages_complete": True,
                "accounts_searched": 2,
                "pages_searched": 5,
            }
            | changes
        )
    )


@pytest.mark.parametrize("missing", ["accounts_complete", "sources_complete", "pages_complete"])
@pytest.mark.parametrize("has_match", [False, True])
def test_incomplete_search_never_becomes_not_found_or_success(make_query, missing, has_match):
    result = classify_payment(
        make_query(), [record()] if has_match else [], coverage(**{missing: False})
    )
    assert result.kind == "failure"
    assert result.code == "CHECKPOINT_FAILED"


def test_complete_empty_search_is_not_found(make_query):
    result = classify_payment(make_query(), [], coverage())
    assert result.kind == "business_outcome"
    assert result.code == "NOT_FOUND"


@pytest.mark.parametrize("status", ["POSTED", "PENDING", "REVERSED"])
def test_unique_exact_match_preserves_displayed_status(make_query, status):
    payment = record(status=status)
    result = classify_payment(make_query(), [payment], coverage())
    assert result.kind == "success"
    assert result.records == [payment]
    assert result.coverage.complete


@pytest.mark.parametrize(
    "change",
    [
        {"member_id": "54321"},
        {"direction": "DEBIT"},
        {"currency": "EUR"},
        {"amount": "250.01"},
        {"transaction_date": "2026-08-31"},
        {"transaction_date": "2026-09-08"},
    ],
)
def test_distractor_is_not_a_match(make_query, change):
    result = classify_payment(make_query(), [record(**change)], coverage())
    assert result.kind == "business_outcome"
    assert result.code == "NOT_FOUND"


@pytest.mark.parametrize("day", [date(2026, 9, 1), date(2026, 9, 7)])
def test_inclusive_transaction_date_boundaries(make_query, day):
    result = classify_payment(make_query(), [record(transaction_date=day)], coverage())
    assert result.kind == "success"


def test_exact_decimal_value_does_not_depend_on_text_padding(make_query):
    result = classify_payment(make_query(), [record(amount="0250.00")], coverage())
    assert result.kind == "success"


def test_reference_comparison_is_exact_and_case_sensitive(make_query):
    records = [record(reference="REF-ONE"), record(reference="ref-one")]
    result = classify_payment(make_query(reference=" REF-ONE "), records, coverage())
    assert result.kind == "success"
    assert result.records[0].reference == "REF-ONE"


def test_repeated_identity_across_sources_is_one_record(make_query):
    records = [record(), record(source="pending", observation_id="obs-two")]
    result = classify_payment(make_query(), records, coverage())
    assert result.kind == "success"
    assert len(result.records) == 1


@pytest.mark.parametrize("change", [{"account_id": "SYNTH-A2"}, {"reference": "REF-TWO"}])
def test_distinct_composite_identities_are_ambiguous(make_query, change):
    result = classify_payment(make_query(), [record(), record(**change)], coverage())
    assert result.kind == "business_outcome"
    assert result.code == "AMBIGUOUS"
    assert len(result.candidates) == 2
    serialized = result.model_dump_json()
    assert "SYNTH-A" not in serialized
    assert "REF-" not in serialized


@pytest.mark.parametrize(
    "change",
    [{"status": "REVERSED"}, {"amount": "249.99"}, {"transaction_date": "2026-09-04"}],
)
def test_conflicting_duplicate_is_not_inferred_as_a_status_transition(make_query, change):
    result = classify_payment(make_query(), [record(), record(**change)], coverage())
    assert result.kind == "failure"
    assert result.code == "INCONSISTENT_RECORD"
