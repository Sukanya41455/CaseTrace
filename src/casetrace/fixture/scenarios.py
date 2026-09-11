from __future__ import annotations

from dataclasses import dataclass

from .data import Account, Transaction

SCENARIOS = (
    "normal",
    "slow-once",
    "timeout",
    "expire-once",
    "permission-denied",
    "stale-member",
    "unknown-dialog",
    "conflicting-record",
    "search-limit",
)


@dataclass
class ScenarioState:
    name: str
    expiration_triggered: bool = False
    slowdown_triggered: bool = False
    transfer_attempts: int = 0

    def __post_init__(self) -> None:
        if self.name not in SCENARIOS:
            raise ValueError(f"unsupported fixture scenario: {self.name}")

    def displayed_member_id(self, requested_member_id: str) -> str:
        if self.name == "stale-member" and requested_member_id != "11111":
            return "54321" if requested_member_id != "54321" else "12345"
        return requested_member_id

    def accounts_for(self, member_id: str, accounts: tuple[Account, ...]) -> tuple[Account, ...]:
        if self.name == "search-limit" and member_id == "12345":
            return accounts + (
                Account("SYNTH-DDA-LIMIT-3"),
                Account("SYNTH-DDA-LIMIT-4"),
            )
        return accounts

    def transactions_for(self, transactions: tuple[Transaction, ...]) -> tuple[Transaction, ...]:
        if self.name != "conflicting-record":
            return transactions
        return transactions + (
            Transaction(
                "12345",
                "SYNTH-DDA-A1",
                "REF-POST-250",
                "CREDIT",
                "250.00",
                "USD",
                "2026-09-03",
                "PENDING",
                "pending",
                "Synthetic conflicting copy",
            ),
        )

    def needs_login(self, session_status: str | None) -> bool:
        if self.name != "expire-once":
            return False
        if not self.expiration_triggered:
            self.expiration_triggered = True
            return True
        return session_status != "reauthenticated"

    def consume_slowdown(self) -> bool:
        if self.name != "slow-once" or self.slowdown_triggered:
            return False
        self.slowdown_triggered = True
        return True
