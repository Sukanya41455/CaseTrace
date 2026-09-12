"""Validate a model-proposed capability against its genuine discovery recording."""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import ValidationError

from .contracts import (
    AssertStep,
    BranchStep,
    BusinessOutcomeCode,
    Capability,
    CheckpointRole,
    ClickStep,
    FillStep,
    ForEachStep,
    NavigateStep,
    PaginateStep,
    ReadStep,
    ReturnStep,
    Step,
    TenantBindings,
)
from .discovery import DiscoveryRecording


class CompilationError(ValueError):
    """Candidate failed a compiler trust or domain-completeness check."""


_PRIMITIVE_STEPS = (NavigateStep, FillStep, ClickStep, ReadStep, AssertStep)


def compile_candidate(
    recording: DiscoveryRecording,
    proposal: Capability | dict[str, object],
    bindings: TenantBindings | None,
) -> Capability:
    """Accept only a schema-valid proposal grounded in the supplied recording."""

    try:
        capability = proposal if isinstance(proposal, Capability) else Capability.model_validate(proposal)
    except ValidationError as error:
        raise CompilationError("candidate does not satisfy the capability schema") from error
    if capability.validation:
        raise CompilationError("a discovery candidate cannot fabricate validation reports")
    if bindings is not None:
        if capability.vendor != bindings.vendor:
            raise CompilationError("candidate vendor does not match tenant bindings")
        if bindings.app_version not in capability.supported_app_versions:
            raise CompilationError("candidate app version does not match tenant bindings")
    _reject_frozen_inputs(capability, recording.sensitive_values)
    _validate_provenance(capability, recording)
    _validate_checkpoint_roles(capability)
    _validate_generalizations(capability, recording.event_ids)
    return capability


def _reject_frozen_inputs(capability: Capability, sensitive_values: tuple[str, ...]) -> None:
    serialized = capability.model_dump_json()
    for value in sensitive_values:
        if value and json.dumps(value) in serialized:
            raise CompilationError("candidate freezes a runtime input value")


def _validate_provenance(capability: Capability, recording: DiscoveryRecording) -> None:
    known = recording.event_ids
    referenced: set[str] = set(capability.provenance.event_ids)
    position = {item.event_id: index for index, item in enumerate(recording.operations)}
    prior_position = -1
    recorded_by_id = {item.event_id: item for item in recording.operations}
    target_by_id = {target.target_id: target for target in capability.targets}
    for step in _walk_steps(capability.steps):
        referenced.update(step.provenance.event_ids)
        if not isinstance(step, _PRIMITIVE_STEPS):
            continue
        if not step.provenance.event_ids:
            if step.provenance.kind == "observed":
                raise CompilationError("observed step is missing event provenance")
            continue
        for event_id in step.provenance.event_ids:
            recorded = recorded_by_id.get(event_id)
            if recorded is None:
                raise CompilationError("step provenance was not present in the recording")
            if step.kind != recorded.step.kind:
                raise CompilationError("step kind does not match its recorded event provenance")
            if not _same_primitive_value(step, recorded.step):
                raise CompilationError("step value does not match its recorded event provenance")
            current_position = position[event_id]
            if current_position < prior_position:
                raise CompilationError("observed action order differs from the recording")
            prior_position = current_position
            if hasattr(step, "target_id"):
                target_id = step.target_id
                if target_id not in target_by_id:
                    raise CompilationError("observed step has an unresolved target")
                if recorded.target is not None and not _semantically_matches_target(
                    target_by_id[target_id], recorded.target
                ):
                    raise CompilationError(
                        "observed target differs semantically from its recorded opaque handle"
                    )
    for handler in capability.handlers:
        referenced.update(handler.provenance.event_ids)
    if referenced - known:
        raise CompilationError("candidate contains invented observed provenance")
    if known - referenced:
        raise CompilationError("candidate omits genuine discovery event provenance")


def _validate_checkpoint_roles(capability: Capability) -> None:
    roles = {
        step.checkpoint_role
        for step in _walk_steps(capability.steps)
        if step.checkpoint_role is not None
    }
    required: set[CheckpointRole] = set()
    for step in _walk_steps(capability.steps):
        if not isinstance(step, ReturnStep):
            continue
        if step.result_kind in {"success", "payment_decision"}:
            required.update(
                {
                    CheckpointRole.MEMBER_IDENTITY_VERIFIED,
                    CheckpointRole.ACCOUNT_IDENTITY_VERIFIED,
                    CheckpointRole.SOURCE_IDENTITY_VERIFIED,
                    CheckpointRole.FILTERS_VERIFIED,
                    CheckpointRole.PAGE_EXHAUSTED,
                    CheckpointRole.SOURCE_EXHAUSTED,
                    CheckpointRole.ACCOUNTS_EXHAUSTED,
                    CheckpointRole.PAYMENT_IDENTITY_VERIFIED,
                }
            )
        elif step.result_kind == "business_outcome":
            code = str(step.result.value)
            required.add(CheckpointRole.MEMBER_IDENTITY_VERIFIED)
            if code == BusinessOutcomeCode.NO_ACCOUNTS.value:
                required.add(CheckpointRole.ACCOUNTS_EXHAUSTED)
            elif code != BusinessOutcomeCode.MEMBER_NOT_FOUND.value:
                required.update(
                    {
                        CheckpointRole.ACCOUNT_IDENTITY_VERIFIED,
                        CheckpointRole.SOURCE_IDENTITY_VERIFIED,
                        CheckpointRole.FILTERS_VERIFIED,
                        CheckpointRole.PAGE_EXHAUSTED,
                        CheckpointRole.SOURCE_EXHAUSTED,
                        CheckpointRole.ACCOUNTS_EXHAUSTED,
                    }
                )
    missing = sorted(role.value for role in required - roles)
    if missing:
        raise CompilationError("candidate is missing required checkpoints: " + ", ".join(missing))


def _validate_generalizations(capability: Capability, recorded_events: set[str]) -> None:
    for step in _walk_steps(capability.steps):
        if isinstance(step, _PRIMITIVE_STEPS) and step.provenance.kind == "authored":
            raise CompilationError("executable UI primitives must be observed or explicitly generalized")
        if step.provenance.kind != "generalized":
            continue
        if not step.provenance.validation_scenario:
            raise CompilationError("generalized operation lacks a required validation scenario")
        if not step.provenance.event_ids or not set(step.provenance.event_ids) <= recorded_events:
            raise CompilationError("generalized operation is not grounded in recorded event provenance")
        if isinstance(step, (ForEachStep, PaginateStep)) and not step.steps:
            raise CompilationError("generalized repetition cannot have an empty body")


def _walk_steps(steps: Iterable[Step]) -> Iterable[Step]:
    for step in steps:
        yield step
        if isinstance(step, BranchStep):
            for case in step.cases:
                yield from _walk_steps(case.steps)
        elif isinstance(step, (ForEachStep, PaginateStep)):
            yield from _walk_steps(step.steps)


def _same_primitive_value(candidate: Step, recorded: Step) -> bool:
    if isinstance(candidate, NavigateStep) and isinstance(recorded, NavigateStep):
        return candidate.url.model_dump() == recorded.url.model_dump()
    if isinstance(candidate, FillStep) and isinstance(recorded, FillStep):
        return candidate.value.model_dump() == recorded.value.model_dump()
    if isinstance(candidate, ReadStep) and isinstance(recorded, ReadStep):
        return candidate.parser == recorded.parser
    if isinstance(candidate, AssertStep) and isinstance(recorded, AssertStep):
        return candidate.predicate.kind == recorded.predicate.kind
    return isinstance(candidate, ClickStep) and isinstance(recorded, ClickStep)


def _semantically_matches_target(candidate, recorded) -> bool:
    if candidate.surface_kind != recorded.surface_kind or candidate.frame_path != recorded.frame_path:
        return False

    def roles(target) -> set[str]:
        values = set()
        for strategy in target.strategies:
            role = getattr(strategy, "role", None) or getattr(strategy, "control_role", None)
            if role:
                values.add(str(role))
        return values

    candidate_roles = roles(candidate)
    recorded_roles = roles(recorded)
    if candidate_roles and recorded_roles and not candidate_roles & recorded_roles:
        return False
    candidate_names = {
        strategy.name
        for strategy in candidate.strategies
        if hasattr(strategy, "name") and isinstance(strategy.name, str)
    }
    recorded_names = {
        strategy.name
        for strategy in recorded.strategies
        if hasattr(strategy, "name") and isinstance(strategy.name, str)
    }
    return not candidate_names or not recorded_names or bool(candidate_names & recorded_names)
