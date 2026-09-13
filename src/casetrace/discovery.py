"""Bounded observe/decide/act discovery with genuine event provenance."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    AssertStep,
    Capability,
    CheckpointRole,
    ClickStep,
    FailureCode,
    FillStep,
    InputValue,
    LiteralValue,
    NavigateStep,
    Observation,
    PaymentQuery,
    Provenance,
    ReadParser,
    ReadStep,
    RunEvent,
    RunStopped,
    Step,
    TargetSpec,
    TenantBindings,
    VariableValue,
    VisiblePredicate,
)
from .evidence import EvidenceWriter
from .provider import (
    CandidateDecision,
    ClickProposal,
    ConfirmProposal,
    FillProposal,
    FinishProposal,
    ModelDecision,
    ModelProvider,
    NavigateProposal,
    ProviderFailure,
    ReadProposal,
    discovery_tool_declarations,
)
from .session import SessionController
from .surface import Surface

MAX_DECISIONS = 40
MAX_ACTIONS = 100
DISCOVERY_TIMEOUT_SECONDS = 600


@dataclass(slots=True)
class RecordedOperation:
    event_id: str
    observation_id: str
    resulting_observation_id: str
    step: Step
    target: TargetSpec | None


@dataclass(slots=True)
class DiscoveryRecording:
    run_id: str
    goal: str
    initial_observation: Observation
    operations: list[RecordedOperation] = field(default_factory=list)
    recognized_surface: Observation | None = None
    provider_calls: dict[str, dict[str, object]] = field(default_factory=dict)
    sensitive_values: tuple[str, ...] = field(default_factory=tuple, repr=False)

    @property
    def event_ids(self) -> set[str]:
        return {operation.event_id for operation in self.operations}

    def recognize_surface(self, observation: Observation) -> None:
        if (
            self.recognized_surface is None
            and observation.vendor is not None
            and observation.app_version is not None
        ):
            self.recognized_surface = observation

    def record_provider_call(
        self, purpose: str, decision: ModelDecision | CandidateDecision
    ) -> None:
        self.provider_calls[purpose] = {
            "response_id": decision.response_id,
            "model_id": decision.model_version,
            "call_index": decision.call_index,
        }

    def public_summary(self) -> dict[str, object]:
        surface = self.recognized_surface or self.initial_observation
        summary = {
            "run_id": self.run_id,
            "goal": self.goal,
            "initial_surface": {
                "vendor": surface.vendor,
                "app_version": surface.app_version,
                "surface_features": surface.surface_features,
            },
            "provider_calls": self.provider_calls,
            "operations": [
                {
                    "event_id": item.event_id,
                    "observation_id": item.observation_id,
                    "resulting_observation_id": item.resulting_observation_id,
                    "step": item.step.model_dump(mode="json"),
                    "target": item.target.model_dump(mode="json") if item.target else None,
                }
                for item in self.operations
            ],
        }
        return _redact_public(summary, self.sensitive_values)


async def discover(
    goal: str,
    args: PaymentQuery,
    surface: Surface,
    session: SessionController,
    provider: ModelProvider,
    evidence: EvidenceWriter,
    *,
    bindings: TenantBindings | None,
    entry_url: str | None = None,
    max_decisions: int = MAX_DECISIONS,
    max_actions: int = MAX_ACTIONS,
    timeout_seconds: float = DISCOVERY_TIMEOUT_SECONDS,
) -> Capability:
    """Run model-guided UI exploration and compile its separately proposed candidate."""

    if max_decisions < 1 or max_actions < 1 or timeout_seconds <= 0:
        raise ValueError("discovery limits must be positive")
    surface.bind_query(args)
    values = tuple(
        str(value)
        for name, value in args.model_dump(mode="json").items()
        if value is not None and name != "currency"
    )
    safe_goal = _redact(goal, values)
    variables: dict[str, object] = {"entry_url": entry_url or (bindings.origin if bindings else "")}
    history: list[dict[str, object]] = [
        {
            "kind": "goal",
            "summary": safe_goal,
            "input_contract": {
                "member_id": "${input.member_id}",
                "amount": "${input.amount}",
                "currency": "${input.currency}",
                "date_from": "${input.date_from}",
                "date_to": "${input.date_to}",
                "reference": "${input.reference}",
            },
        }
    ]

    def emit(**fields: Any) -> str:
        seq = evidence.next_seq
        event_id = fields.pop("event_id", f"event-{seq}")
        evidence.emit(
            RunEvent(
                event_id=event_id,
                run_id=session.run_id,
                seq=seq,
                timestamp=datetime.now(UTC),
                actor="automation",
                ownership_generation=session.ownership_generation,
                browser_id=session.browser_id,
                context_id=session.context_id,
                page_id=session.page_id,
                evidence_refs=[],
                **fields,
            )
        )
        return event_id

    recording: DiscoveryRecording | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            observation = await surface.observe()
            emit(
                kind="run_started",
                summary="Model-guided UI discovery started with symbolic typed inputs",
                observation_id=observation.observation_id,
            )
            recording = DiscoveryRecording(
                run_id=session.run_id,
                goal=safe_goal,
                initial_observation=observation,
                sensitive_values=values,
            )
            recording.recognize_surface(observation)
            action_count = 0
            for _ in range(max_decisions):
                try:
                    decision = await provider.decide(
                        _provider_observation(observation),
                        history,
                        discovery_tool_declarations(),
                    )
                except ProviderFailure as error:
                    raise RunStopped(
                        FailureCode.MODEL_ERROR,
                        None,
                        "one valid declared model tool call",
                        "provider request failed",
                    ) from error
                if isinstance(decision.proposal, FinishProposal):
                    recording.record_provider_call("finish", decision)
                    emit(
                        kind="checkpoint",
                        summary="Model requested candidate compilation",
                        observation_id=observation.observation_id,
                        provider_response_id=decision.response_id,
                        model_id=decision.model_version,
                        model_call_count=decision.call_index,
                    )
                    _persist_recording(recording, evidence.run_dir)
                    if not recording.operations:
                        raise RunStopped(
                            FailureCode.MODEL_ERROR,
                            None,
                            "observed executable operations",
                            "model finished before acting",
                        )
                    if not _has_executable_goal_confirmation(recording, observation):
                        raise RunStopped(
                            FailureCode.CHECKPOINT_FAILED,
                            None,
                            "fresh executable payment confirmation on the "
                            "transaction detail screen",
                            "UI goal was not confirmed",
                        )
                    from .compiler import compile_candidate

                    try:
                        candidate = await provider.propose_candidate(
                            recording.public_summary(), Capability.model_json_schema()
                        )
                    except ProviderFailure as error:
                        raise RunStopped(
                            FailureCode.MODEL_ERROR,
                            None,
                            "schema-valid candidate proposal",
                            "provider candidate request failed",
                        ) from error
                    recording.record_provider_call("candidate", candidate)
                    emit(
                        kind="checkpoint",
                        summary="Model capability candidate received",
                        observation_id=observation.observation_id,
                        provider_response_id=candidate.response_id,
                        model_id=candidate.model_version,
                        model_call_count=candidate.call_index,
                    )
                    _persist_recording(recording, evidence.run_dir)
                    return compile_candidate(recording, candidate.candidate, bindings)
                action_count += 1
                if action_count > max_actions:
                    raise RunStopped(
                        FailureCode.MODEL_LIMIT,
                        None,
                        "at most 100 UI actions",
                        "action budget exhausted",
                    )
                prior = observation
                step, target, result_summary = await _execute_proposal(
                    decision,
                    prior,
                    args,
                    variables,
                    surface,
                    session,
                    action_count,
                )
                observation = await surface.observe()
                recording.recognize_surface(observation)
                event_id = emit(
                    kind="action",
                    summary=_redact(decision.proposal.purpose, values),
                    step_id=step.step_id,
                    observation_id=observation.observation_id,
                    provider_response_id=decision.response_id,
                    model_id=decision.model_version,
                    model_call_count=decision.call_index,
                )
                observed_step = step.model_copy(
                    update={"provenance": Provenance(kind="observed", event_ids=[event_id])}
                )
                recording.operations.append(
                    RecordedOperation(
                        event_id=event_id,
                        observation_id=prior.observation_id,
                        resulting_observation_id=observation.observation_id,
                        step=observed_step,
                        target=target,
                    )
                )
                _persist_recording(recording, evidence.run_dir)
                history.append(
                    {
                        "kind": "action_result",
                        "action": decision.proposal.kind,
                        "purpose": _redact(decision.proposal.purpose, values),
                        "outcome": result_summary,
                        "observation_id": observation.observation_id,
                    }
                )
            raise RunStopped(
                FailureCode.MODEL_LIMIT,
                None,
                "at most 40 model decisions",
                "decision budget exhausted",
            )
    except TimeoutError as error:
        if recording is not None:
            _persist_recording(recording, evidence.run_dir)
        raise RunStopped(
            FailureCode.MODEL_LIMIT,
            None,
            "discovery within active deadline",
            "discovery deadline exhausted",
        ) from error
    except BaseException:
        if recording is not None:
            _persist_recording(recording, evidence.run_dir)
        raise


async def _execute_proposal(
    decision: ModelDecision,
    observation: Observation,
    args: PaymentQuery,
    variables: dict[str, object],
    surface: Surface,
    session: SessionController,
    ordinal: int,
) -> tuple[Step, TargetSpec | None, str]:
    proposal = decision.proposal
    authored = Provenance(kind="authored", validation_scenario="discovery-in-progress")
    target: TargetSpec | None = None
    if isinstance(proposal, NavigateProposal):
        if proposal.url_kind == "entry":
            url = VariableValue(name="entry_url")
        elif proposal.url is not None:
            url = LiteralValue(value=proposal.url)
        else:
            raise RunStopped(
                FailureCode.MODEL_ERROR,
                None,
                "literal URL when url_kind is literal",
                "invalid navigation proposal",
            )
        step = NavigateStep(
            step_id=f"discovery-{ordinal}",
            url=url,
            checks=[],
            checkpoint=None,
            provenance=authored,
        )
        await session.execute(lambda: surface.act(step, args, variables))
        return step, None, "navigation delivered and a fresh sanitized observation captured"
    if isinstance(proposal, (FillProposal, ClickProposal, ReadProposal, ConfirmProposal)):
        target = surface.target_for_handle(observation.observation_id, proposal.target_handle)
    if isinstance(proposal, FillProposal):
        value = (
            InputValue(name=proposal.input_name)
            if proposal.value_kind == "input" and proposal.input_name is not None
            else LiteralValue(value=proposal.literal_value)
        )
        step = FillStep(
            step_id=f"discovery-{ordinal}",
            target_id=proposal.target_handle,
            value=value,
            checks=[],
            checkpoint=None,
            provenance=authored,
        )
        await session.execute(lambda: surface.act(step, args, variables))
        return (
            step,
            target,
            {
                "summary": "bounded value source delivered; fresh sanitized observation captured",
                "value_ref": value.model_dump(mode="json"),
            },
        )
    if isinstance(proposal, ClickProposal):
        step = ClickStep(
            step_id=f"discovery-{ordinal}",
            target_id=proposal.target_handle,
            checks=[],
            checkpoint=None,
            provenance=authored,
        )
        await session.execute(lambda: surface.act(step, args, variables))
        return step, target, "control activated and a fresh sanitized observation captured"
    if isinstance(proposal, ReadProposal):
        step = ReadStep(
            step_id=f"discovery-{ordinal}",
            target_id=proposal.target_handle,
            parser=ReadParser(proposal.parser),
            store_as=proposal.store_as,
            checks=[],
            checkpoint=None,
            provenance=authored,
        )
        value = await session.execute(lambda: surface.read(step, args, variables))
        variables[proposal.store_as] = value
        if isinstance(value, list):
            outcome = {
                "summary": f"read {len(value)} rows into private run-local state",
                "private_value_shape": {
                    "variable": proposal.store_as,
                    "kind": "collection",
                    "count": len(value),
                    "item_fields": sorted(
                        {str(key) for row in value if isinstance(row, dict) for key in row}
                    ),
                },
            }
        elif isinstance(value, dict):
            outcome = {
                "summary": f"read {len(value)} fields into private run-local state",
                "private_value_shape": {
                    "variable": proposal.store_as,
                    "kind": "mapping",
                    "fields": sorted(str(key) for key in value),
                },
            }
        else:
            outcome = {
                "summary": "read one value into private run-local state",
                "private_value_shape": {
                    "variable": proposal.store_as,
                    "kind": "scalar",
                },
            }
        return step, target, outcome
    if isinstance(proposal, ConfirmProposal):
        predicate = VisiblePredicate(target_id=proposal.target_handle)
        step = AssertStep(
            step_id=f"discovery-{ordinal}",
            predicate=predicate,
            checks=[predicate],
            checkpoint=f"checkpoint-{ordinal}" if proposal.checkpoint_role else None,
            checkpoint_role=(
                CheckpointRole(proposal.checkpoint_role) if proposal.checkpoint_role else None
            ),
            provenance=authored,
        )
        passed = await session.execute(lambda: surface.check(predicate, args, variables))
        if not passed:
            raise RunStopped(
                FailureCode.CHECKPOINT_FAILED,
                step.step_id,
                "visible confirmed target",
                "confirmation false",
            )
        return step, target, "executable visible-state check passed"
    raise RunStopped(
        FailureCode.MODEL_ERROR,
        None,
        "supported discovery proposal",
        "invalid proposal",
    )


def _redact(value: str, sensitive_values: tuple[str, ...]) -> str:
    safe = value
    for sensitive in sorted(sensitive_values, key=len, reverse=True):
        safe = safe.replace(sensitive, "[REDACTED]")
    return safe[:500]


def _redact_public(value: Any, sensitive_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _redact(value, sensitive_values)
    if isinstance(value, dict):
        return {str(key): _redact_public(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_public(item, sensitive_values) for item in value]
    return value


def _persist_recording(recording: DiscoveryRecording, run_dir: Path) -> None:
    destination = Path(run_dir) / "recording.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(recording.public_summary(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _has_executable_goal_confirmation(
    recording: DiscoveryRecording, observation: Observation
) -> bool:
    if observation.state.get("screen") != "transaction_detail":
        return False
    if not recording.operations:
        return False
    latest = recording.operations[-1]
    return (
        latest.resulting_observation_id == observation.observation_id
        and isinstance(latest.step, AssertStep)
        and latest.step.checkpoint_role is CheckpointRole.PAYMENT_IDENTITY_VERIFIED
        and bool(latest.step.checks)
    )


def _provider_observation(observation: Observation) -> Observation:
    """Add safe ordinals to duplicate controls without revealing row values."""

    totals: dict[tuple[str, str], int] = {}
    for control in observation.controls:
        key = (control.role, control.label)
        totals[key] = totals.get(key, 0) + 1
    seen: dict[tuple[str, str], int] = {}
    controls = []
    for control in observation.controls:
        key = (control.role, control.label)
        seen[key] = seen.get(key, 0) + 1
        label = control.label
        if totals[key] > 1:
            label = f"{label} [occurrence {seen[key]} of {totals[key]}]"
        controls.append(control.model_copy(update={"label": label}))
    return observation.model_copy(update={"controls": controls})
