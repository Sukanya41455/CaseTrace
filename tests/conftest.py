from collections.abc import Callable

import pytest

from casetrace.contracts import PaymentQuery

BASE_QUERY = {
    "member_id": "12345",
    "amount": "250.00",
    "currency": "USD",
    "date_from": "2026-09-01",
    "date_to": "2026-09-07",
    "reference": None,
}


@pytest.fixture
def make_query() -> Callable[..., PaymentQuery]:
    def factory(**changes: object) -> PaymentQuery:
        return PaymentQuery.model_validate(BASE_QUERY | changes)

    return factory
