"""Bounded, model-free execution of a validated capability against rendered UI."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .contracts import (
    AbsentPredicate,
    AssertStep,
    BranchStep,
    BusinessDecision,
    BusinessOutcome,
    BusinessOutcomeCode,
    Capability,
    ClickStep,
    Failure,
    FailureCode,
    FailureDecision,
    FillStep,
    ForEachStep,
    MatchDecision,
    NavigateStep,
    PaginateStep,
    PaymentDecisionBindings,
    PaymentFieldBindings,
    PaymentQuery,
    PaymentRecord,
    ReadRole,
    ReadStep,
    ReturnStep,
    RunEvent,
    RunResult,
    RunStopped,
    SearchCoverage,
    Success,
    TenantBindings,
    VisibleTextStrategy,
    resolve_value,
)
from .evidence import EvidenceWriter
from .integrity import binding_digest, capability_digest
from .matching import classify_payment
from .session import SessionController
from .surface import Surface


def _steps(steps):
    for step in steps:
        yield step
        if isinstance(step, (ForEachStep, PaginateStep)):
            yield from _steps(step.steps)
        elif isinstance(step, BranchStep):
            for case in step.cases:
                yield from _steps(case.steps)


class _Returned(Exception):
    def __init__(self, result):
        self.result = result


class ReplayRunner:
    def __init__(self, capability, args, bindings, surface, session, evidence):
        self.capability: Capability = capability
        self.args: PaymentQuery = args
        self.bindings: TenantBindings = bindings
        self.surface: Surface = surface
        self.session: SessionController = session
        self.evidence: EvidenceWriter = evidence
        self.variables: dict[str, Any] = {"entry_url": bindings.origin + "/"}
        self.current_step = None
        self.action_count = 0
        self.attempt_count = 0
        self.accounts: set[str] | None = None
        self.account_collections: list[list] = []
        self.accounts_complete = False
        self.exhausted: set[tuple[str, str]] = set()
        self.pages: set[tuple[str, str, str]] = set()
        self.records: dict[str, list[PaymentRecord]] = defaultdict(list)
        self.details: dict[str, list[PaymentRecord]] = defaultdict(list)
        self.fields: list[tuple[str, dict]] = []
        self.page_reads: list[set[tuple[str, str]]] = []
        self.metadata = {
            "run_id": session.run_id,
            "artifact_digest": capability_digest(capability),
            "binding_digest": binding_digest(bindings),
            "evidence_refs": ["events.jsonl"],
        }

    def stop(self, code=FailureCode.CHECKPOINT_FAILED, expected="verified UI checkpoint"):
        raise RunStopped(code, self.current_step, expected, "required evidence unavailable")

    def emit(self, kind, summary, **fields):
        seq = self.evidence.next_seq
        self.evidence.emit(
            RunEvent(
                event_id=f"replay-{seq}",
                run_id=self.session.run_id,
                seq=seq,
                timestamp=datetime.now(UTC),
                kind=kind,
                actor="automation",
                summary=summary,
                step_id=self.current_step,
                model_call_count=0,
                browser_id=self.session.browser_id,
                context_id=self.session.context_id,
                page_id=self.session.page_id,
                ownership_generation=self.session.ownership_generation,
                **fields,
            )
        )

    def coverage(self):
        expected = {
            (account, source)
            for account in self.accounts or ()
            for source in ("history", "pending")
        }
        return SearchCoverage(
            accounts_complete=self.accounts_complete,
            sources_complete=self.accounts is not None and expected <= self.exhausted,
            pages_complete=self.accounts is not None and expected <= self.exhausted,
            accounts_searched=len({account for account, _ in self.exhausted}),
            pages_searched=len(self.pages),
        )

    def validate_seal(self):
        required = {
            p.validation_scenario
            for p in [self.capability.provenance]
            + [s.provenance for s in _steps(self.capability.steps)]
            + [h.provenance for h in self.capability.handlers]
            if p.validation_scenario is not None
        }
        passed = set()
        for report in self.capability.validation:
            if (
                report.artifact_digest == self.metadata["artifact_digest"]
                and report.binding_digest == self.metadata["binding_digest"]
            ):
                passed.update(
                    s.scenario for s in report.scenario_results if s.passed and s.evidence_refs
                )
        if not passed or not required <= passed:
            self.stop(FailureCode.UNSUPPORTED_VERSION, "validation for exact artifact and bindings")

    async def observation(self, *, allow_blank=False):
        observation = await self.session.execute(self.surface.observe)
        if allow_blank and observation.vendor is None:
            return observation
        if (
            observation.vendor != self.capability.vendor
            or observation.vendor != self.bindings.vendor
            or observation.app_version != self.bindings.app_version
            or observation.app_version not in self.capability.supported_app_versions
        ):
            self.stop(FailureCode.UNSUPPORTED_VERSION, "supported visible vendor and app version")
        if not set(self.capability.required_surface_features) <= set(observation.surface_features):
            self.stop(FailureCode.UNSUPPORTED_SURFACE, "required surface features")
        if observation.state.get("dialog_present"):
            self.stop(FailureCode.UNKNOWN_STATE, "recognized non-dialog state")
        return observation

    async def check(self, predicate):
        return await self.session.execute(
            lambda: self.surface.check(predicate, self.args, self.variables)
        )

    async def checks(self, predicates):
        for predicate in predicates:
            if not await self.check(predicate):
                self.stop()

    async def sequence(self, steps):
        for step in steps:
            self.current_step = step.step_id
            before = await self.observation(allow_blank=isinstance(step, NavigateStep))
            for handler in self.capability.handlers:
                if await self.check(handler.detector):
                    # Recovery is never inferred from an unmatched screen or arbitrary exception.
                    self.stop(
                        handler.failure_code, f"handler {handler.handler_id} requires recovery"
                    )
            if isinstance(step, (NavigateStep, FillStep, ClickStep)):
                self.action_count += 1
                if self.action_count > self.capability.limits.max_ui_actions:
                    self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "bounded UI actions")
                async with asyncio.timeout(self.capability.limits.action_timeout_seconds):
                    await self.session.execute(
                        lambda: self.surface.act(step, self.args, self.variables)
                    )
                await self.observation()
                self.emit("action", f"Executed declared {step.kind}")
            elif isinstance(step, ReadStep):
                async with asyncio.timeout(self.capability.limits.action_timeout_seconds):
                    value = await self.session.execute(
                        lambda: self.surface.read(step, self.args, self.variables)
                    )
                after = await self.observation()
                if before.state.get("page_fingerprint") != after.state.get("page_fingerprint"):
                    self.stop(expected="unchanged page during read")
                self.variables[step.store_as] = value
                self.consume(step, value, after)
                self.emit(
                    "observation", "Read declared visible data", observation_id=after.observation_id
                )
            elif isinstance(step, AssertStep):
                await self.checks([step.predicate])
            elif isinstance(step, BranchStep):
                matches = [case for case in step.cases if await self.check(case.guard)]
                if len(matches) != 1:
                    self.stop(
                        step.fallback_failure if not matches else FailureCode.UNKNOWN_STATE,
                        "exactly one declared branch",
                    )
                await self.sequence(matches[0].steps)
            elif isinstance(step, ForEachStep):
                collection = resolve_value(step.collection, self.args, self.variables)
                if not isinstance(collection, list):
                    self.stop(expected="observed collection")
                if len(collection) > step.max_items:
                    self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "bounded collection")
                accounts_loop = any(collection is value for value in self.account_collections)
                parent = self.variables.copy()
                for item in tuple(collection):
                    self.variables = parent | {step.item_variable: item}
                    await self.sequence(step.steps)
                self.variables = parent
                if accounts_loop:
                    self.accounts_complete = True
            elif isinstance(step, PaginateStep):
                await self.paginate(step)
            elif isinstance(step, ReturnStep):
                await self.checks(step.checks)
                raise _Returned(self.terminal(step))
            self.current_step = step.step_id
            await self.checks(step.checks)
            if step.checkpoint:
                self.emit("checkpoint", "Declared checkpoint passed")

    def current_fields(self, fingerprint):
        merged = {}
        for read_fingerprint, fields in self.fields:
            if read_fingerprint == fingerprint:
                merged.update(fields)
        return merged

    def consume(self, step, value, observation):
        fingerprint = observation.state.get("page_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            self.stop(expected="visible page fingerprint")
        if isinstance(value, dict):
            self.fields.append((fingerprint, value))
        context = self.current_fields(fingerprint)
        if step.read_role == ReadRole.ACCOUNTS:
            if context.get("member_id") != self.args.member_id or not isinstance(value, list):
                self.stop(expected="visible member identity before account enumeration")
            ids = [row.get("account_id") for row in value if isinstance(row, dict)]
            if len(ids) != len(value) or len(set(ids)) != len(ids) or not all(ids):
                self.stop(expected="distinct observed account IDs")
            if len(ids) > self.capability.limits.max_accounts:
                self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "bounded accounts")
            if self.accounts is not None and self.accounts != set(ids):
                self.stop(FailureCode.INCONSISTENT_RECORD, "stable account enumeration")
            self.accounts = set(ids)
            self.account_collections.append(value)
        elif step.read_role == ReadRole.PAYMENT_ROWS:
            account = resolve_value(step.account, self.args, self.variables)
            source = resolve_value(step.source, self.args, self.variables)
            expected = self.args.model_dump(mode="json") | {
                "account_id": account,
                "source": source,
                "direction": "CREDIT",
                "reference": self.args.reference or "",
            }
            if (
                account not in (self.accounts or ())
                or source not in {"history", "pending"}
                or any(context.get(key) != val for key, val in expected.items())
            ):
                self.stop(expected="fresh member/account/source identity and applied query filters")
            if not self.page_reads or not isinstance(value, list):
                self.stop(expected="payment rows inside bounded pagination")
            if len(value) > self.capability.limits.max_rows_per_page:
                self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "bounded rows")
            for row in value:
                record = self.record(
                    row
                    | {"member_id": self.args.member_id, "account_id": account, "source": source},
                    observation,
                )
                self.records[step.step_id].append(record)
            self.page_reads[-1].add((account, source))
            self.pages.add((account, source, fingerprint))
        elif step.read_role == ReadRole.PAYMENT_DETAIL:
            if not isinstance(value, dict):
                self.stop(expected="visible payment detail fields")
            record = self.record(value, observation)
            if record.member_id != self.args.member_id or record.account_id not in (
                self.accounts or ()
            ):
                self.stop(expected="payment identity in enumerated member accounts")
            value["observation_id"] = observation.observation_id
            self.details[step.step_id].append(record)

    def record(self, row, observation):
        fields = {key: val for key, val in row.items() if key in PaymentRecord.model_fields}
        fields["observation_id"] = observation.observation_id
        if "source" in fields:
            fields["source"] = str(fields["source"]).lower()
        try:
            return PaymentRecord.model_validate(fields)
        except ValidationError:
            self.stop(expected="complete typed payment fields")

    async def paginate(self, step):
        fingerprints = set()
        pair = None
        parent = self.variables.copy()
        for _ in range(min(step.max_pages, self.capability.limits.max_pages_per_source)):
            observation = await self.observation()
            fingerprint = observation.state.get("page_fingerprint")
            if not fingerprint or fingerprint in fingerprints:
                self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "distinct next page")
            fingerprints.add(fingerprint)
            self.page_reads.append(set())
            self.variables = parent.copy()
            await self.sequence(step.steps)
            read_pairs = self.page_reads.pop()
            if len(read_pairs) != 1 or (pair is not None and read_pairs != {pair}):
                self.stop(expected="one consistent source read on every page")
            pair = next(iter(read_pairs))
            after = await self.observation()
            if after.state.get("page_fingerprint") != fingerprint:
                self.stop(expected="return to the same observed page before pagination")
            terminal = await self.check(step.until)
            next_absent = await self.check(AbsentPredicate(target_id=step.next_target_id))
            if terminal and next_absent:
                self.exhausted.add(pair)
                self.variables = parent
                return
            if terminal != next_absent:
                self.stop(expected="terminal predicate agrees with next-page state")
            if len(fingerprints) >= min(
                step.max_pages, self.capability.limits.max_pages_per_source
            ):
                break
            click = ClickStep(
                step_id=step.step_id,
                target_id=step.next_target_id,
                checks=[],
                checkpoint=None,
                provenance=step.provenance,
            )
            self.action_count += 1
            if self.action_count > self.capability.limits.max_ui_actions:
                self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "bounded UI actions")
            await self.session.execute(lambda: self.surface.act(click, self.args, self.variables))
            self.emit("action", "Advanced declared pagination target")
        self.stop(FailureCode.SEARCH_LIMIT_EXCEEDED, "exhausted source within page limit")

    def terminal(self, step):
        if step.result_kind == "failure":
            self.stop(FailureCode(step.result.value), "declared failure outcome")
        if step.result_kind == "business_outcome" and step.result.value in {
            "MEMBER_NOT_FOUND",
            "NO_ACCOUNTS",
        }:
            expected = {
                "MEMBER_NOT_FOUND": "Member not found",
                "NO_ACCOUNTS": "No deposit accounts",
            }
            # Require a direct, executed visible-state proof, not a constant equality.
            checked = {p.target_id for p in step.checks if p.kind == "visible"}
            if not any(
                t.target_id in checked
                and any(
                    isinstance(s, VisibleTextStrategy)
                    and s.exact
                    and s.text == expected[step.result.value]
                    for s in t.strategies
                )
                for t in self.capability.targets
            ):
                self.stop(expected="explicit visible business outcome")
            return BusinessOutcome(
                code=BusinessOutcomeCode(step.result.value),
                coverage=self.coverage(),
                **self.metadata,
            )
        if isinstance(step.result, PaymentDecisionBindings):
            rows = [record for key in step.result.record_steps for record in self.records[key]]
            details = [record for key in step.result.detail_steps for record in self.details[key]]
        else:
            rows = [record for records in self.records.values() for record in records]
            details = [record for records in self.details.values() for record in records]
        decision = classify_payment(self.args, rows, self.coverage())
        if isinstance(decision, FailureDecision):
            self.stop(decision.code, "complete consistent search")
        if isinstance(decision, BusinessDecision):
            if step.result_kind != "payment_decision" and (
                step.result_kind != "business_outcome" or step.result.value != decision.code
            ):
                self.stop(expected="declared result agrees with observed matching decision")
            return BusinessOutcome(
                code=decision.code,
                coverage=decision.coverage,
                candidates=decision.candidates,
                **self.metadata,
            )
        assert isinstance(decision, MatchDecision)
        matched = decision.records[0]
        semantic = lambda record: record.model_dump(exclude={"source", "observation_id"})
        candidates = [
            record
            for record in details
            if (record.member_id, record.account_id, record.reference)
            == (matched.member_id, matched.account_id, matched.reference)
        ]
        if not candidates:
            self.stop(expected="observed detail for unique matched payment")
        if any(semantic(record) != semantic(matched) for record in candidates):
            self.stop(FailureCode.INCONSISTENT_RECORD, "detail agrees with observed rows")
        if isinstance(step.result, PaymentFieldBindings):
            extracted = PaymentRecord.model_validate(
                {name: resolve_value(ref, self.args, self.variables) for name, ref in step.result}
            )
            if semantic(extracted) != semantic(matched):
                self.stop(FailureCode.INCONSISTENT_RECORD, "declared output agrees with detail")
        elif step.result_kind != "payment_decision":
            self.stop(expected="success output binding")
        return Success(payment=candidates[0], observed_at=datetime.now(UTC), **self.metadata)


async def replay(
    capability: Capability,
    args: PaymentQuery,
    bindings: TenantBindings,
    surface: Surface,
    session: SessionController,
    evidence: EvidenceWriter,
    *,
    validation_mode: bool = False,
) -> RunResult:
    runner = ReplayRunner(capability, args, bindings, surface, session, evidence)
    runner.emit("run_started", "Deterministic replay started")
    try:
        if not validation_mode:
            runner.validate_seal()
        surface.bind_query(args)
        surface.set_targets(capability.targets)
        async with asyncio.timeout(capability.limits.active_timeout_seconds):
            await runner.sequence(capability.steps)
        runner.stop(expected="typed terminal result")
    except _Returned as returned:
        result = returned.result
    except (RunStopped, TimeoutError, ValidationError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, RunStopped):
            code, expected, observed = error.code, error.expected, error.observed
        else:
            code = (
                FailureCode.TIMEOUT
                if isinstance(error, TimeoutError)
                else FailureCode.CHECKPOINT_FAILED
            )
            expected, observed = "bounded typed execution", "execution stopped"
        result = Failure(
            code=code,
            current_step=runner.current_step,
            expected_condition=expected,
            observed_condition=observed,
            attempt_count=runner.attempt_count,
            **runner.metadata,
        )
    runner.emit("result", f"Replay ended with {result.kind}")
    evidence.finish(result)
    return result
