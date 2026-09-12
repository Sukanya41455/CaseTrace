"""Model-neutral surface contract and policy-safe resolved target metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import Observation, PaymentQuery, Predicate, Step, TargetSpec


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """Non-browser metadata used by policy before a control can be activated."""

    target_id: str
    count: int
    control_meaning: str
    page_url: str
    form_action: str | None = None
    form_method: str | None = None


class Surface(Protocol):
    async def observe(self) -> Observation: ...

    async def act(self, step: Step, args: PaymentQuery, variables: dict[str, object]) -> None: ...

    async def read(
        self, step: Step, args: PaymentQuery, variables: dict[str, object]
    ) -> object: ...

    async def check(
        self, predicate: Predicate, args: PaymentQuery, variables: dict[str, object]
    ) -> bool: ...

    async def capture(self) -> dict[str, object]: ...

    def bind_query(self, args: PaymentQuery) -> None: ...

    def observed_targets(self, observation_id: str | None = None) -> list[TargetSpec]: ...

    def target_for_handle(self, observation_id: str, handle: str) -> TargetSpec: ...
