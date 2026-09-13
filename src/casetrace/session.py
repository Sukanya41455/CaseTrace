"""Exclusive browser-session ownership and human handoff state transitions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from .contracts import FailureCode, Intervention, RunStopped


class SessionState(StrEnum):
    AUTOMATION = "AUTOMATION"
    PAUSING = "PAUSING"
    HUMAN = "HUMAN"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ResumeVerifier = Callable[[], Awaitable[bool | tuple[bool, str]]]
TransitionHandler = Callable[[SessionState], Awaitable[None]]


class SessionController:
    def __init__(
        self,
        run_id: str,
        *,
        browser_id: str | None = None,
        context_id: str | None = None,
        page_id: str | None = None,
        resume_verifier: ResumeVerifier | None = None,
        transition_handler: TransitionHandler | None = None,
    ) -> None:
        self.run_id = run_id
        self.browser_id = browser_id or f"browser-{uuid4().hex}"
        self.context_id = context_id or f"context-{uuid4().hex}"
        self.page_id = page_id or f"page-{uuid4().hex}"
        self.state = SessionState.AUTOMATION
        self.owner = "automation"
        self.ownership_generation = 0
        self._state_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self._transition_lock = asyncio.Lock()
        self._intervention: Intervention | None = None
        self._resume_verifier = resume_verifier
        self._transition_handler = transition_handler

    @property
    def intervention(self) -> Intervention | None:
        return self._intervention

    async def execute(self, operation: Callable[[], Awaitable[object]]) -> object:
        async with self._state_lock:
            self._require_automation()
            generation = self.ownership_generation
        async with self._action_lock:
            async with self._state_lock:
                self._require_automation()
                if generation != self.ownership_generation:
                    self._stopped("ownership generation changed")
            result = await operation()
            async with self._state_lock:
                if (
                    self.state is not SessionState.AUTOMATION
                    or self.owner != "automation"
                    or generation != self.ownership_generation
                ):
                    self._stopped("late result from prior ownership generation")
            return result

    async def request_handoff(self, reason: str, step_id: str | None) -> Intervention:
        async with self._transition_lock:
            async with self._state_lock:
                self._require_automation()
                self.state = SessionState.PAUSING
                self.owner = "system"
                self.ownership_generation += 1
                self._intervention = Intervention(
                    intervention_id=f"intervention-{uuid4().hex}",
                    run_id=self.run_id,
                    state="waiting_human",
                    reason=reason,
                    step_id=step_id,
                    ownership_generation=self.ownership_generation,
                    created_at=datetime.now(UTC),
                )
            await self._notify_transition()
            return self._intervention

    async def take_control(self, intervention_id: str) -> None:
        async with self._action_lock:
            async with self._transition_lock:
                async with self._state_lock:
                    intervention = self._matching_intervention(intervention_id)
                    if (
                        self.state is not SessionState.PAUSING
                        or intervention.state != "waiting_human"
                    ):
                        raise ValueError("intervention is not waiting for human control")
                    self.state = SessionState.HUMAN
                    self.owner = "system"
                    self.ownership_generation += 1
                    self._intervention = intervention.model_copy(
                        update={
                            "state": "human",
                            "ownership_generation": self.ownership_generation,
                        }
                    )
                await self._notify_transition()
                self.owner = "human"

    async def resume(self, intervention_id: str) -> None:
        async with self._action_lock:
            async with self._transition_lock:
                async with self._state_lock:
                    intervention = self._matching_intervention(intervention_id)
                    if self.state is not SessionState.HUMAN or intervention.state != "human":
                        raise ValueError("intervention is not under human control")
                    self.state = SessionState.VERIFYING
                    self.owner = "system"
                    self.ownership_generation += 1
                    generation = self.ownership_generation
                await self._notify_transition()
            verified: bool | tuple[bool, str] = False
            if self._resume_verifier is not None:
                try:
                    verified = await self._resume_verifier()
                except Exception:
                    verified = False
            allowed, reason = (
                verified if isinstance(verified, tuple) else (verified, "resume rejected")
            )
            async with self._transition_lock:
                async with self._state_lock:
                    if (
                        self.state is not SessionState.VERIFYING
                        or generation != self.ownership_generation
                    ):
                        self._stopped("ownership changed during resume verification")
                    self.state = SessionState.AUTOMATION if allowed else SessionState.HUMAN
                    self.ownership_generation += 1
                    self._intervention = intervention.model_copy(
                        update={
                            "state": "resumed" if allowed else "human",
                            "reason": intervention.reason if allowed else reason,
                            "ownership_generation": self.ownership_generation,
                        }
                    )
                await self._notify_transition()
                self.owner = "automation" if allowed else "human"
                if not allowed:
                    raise RunStopped(
                        FailureCode.CHECKPOINT_FAILED,
                        intervention.step_id,
                        "recognized permitted resume checkpoint",
                        "resume verification failed",
                    )

    async def abort(self) -> None:
        async with self._transition_lock:
            await self._cancel_ownership()
            await self._notify_transition()

    async def _cancel_ownership(self) -> None:
        async with self._state_lock:
            self.state = SessionState.CANCELLED
            self.owner = "system"
            self.ownership_generation += 1
            if self._intervention is not None:
                self._intervention = self._intervention.model_copy(
                    update={"state": "aborted", "ownership_generation": self.ownership_generation}
                )

    async def _notify_transition(self) -> None:
        # Serialize hooks, but never hold the state lock across application callbacks.
        # Hooks may inspect state; they must not recursively initiate another transition.
        if self._transition_handler is not None:
            try:
                await self._transition_handler(self.state)
            except BaseException as error:
                await self._cancel_ownership()
                try:
                    await self._transition_handler(SessionState.CANCELLED)
                except Exception:
                    pass  # Ownership is revoked even if the browser is already lost.
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise RunStopped(
                    FailureCode.CANCELLED,
                    None,
                    "ownership transition completed",
                    "transition failed",
                ) from None

    def _matching_intervention(self, intervention_id: str) -> Intervention:
        if self._intervention is None or self._intervention.intervention_id != intervention_id:
            raise ValueError("unknown intervention")
        return self._intervention

    def _require_automation(self) -> None:
        if self.state is not SessionState.AUTOMATION or self.owner != "automation":
            self._stopped("automation does not own the session")

    @staticmethod
    def _stopped(observed: str) -> None:
        raise RunStopped(
            FailureCode.CANCELLED,
            None,
            "automation ownership at current generation",
            observed,
        )
