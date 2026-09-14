"""Validated, data-only contracts shared by discovery and deterministic replay."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[\w.-]+$")]
ModelIdentifier = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[\w.:-]+$")
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Money = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]{2}$")]


class ContractModel(BaseModel):
    """Base for artifact and runtime contracts: unknown fields are always errors."""

    model_config = ConfigDict(extra="forbid")


def _validate_positive_money(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("amount must be an exact decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("amount must be an exact decimal string") from error
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent != -2:
        raise ValueError("amount must be positive with exactly two fractional digits")
    return value


class PaymentQuery(ContractModel):
    member_id: Annotated[str, StringConstraints(pattern=r"^\d{5}$")]
    amount: Money
    currency: Literal["USD"]
    date_from: date
    date_to: date
    reference: Annotated[str, StringConstraints(max_length=40)] | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> object:
        return _validate_positive_money(value)  # type: ignore[arg-type]

    @field_validator("reference", mode="before")
    @classmethod
    def normalize_reference(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("reference must be a string or null")
        normalized = value.strip()
        if not normalized:
            raise ValueError("reference cannot be empty")
        return normalized

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def require_date_only(cls, value: object) -> object:
        if not isinstance(value, (str, date)) or isinstance(value, datetime):
            raise ValueError("dates must be ISO calendar dates without times")
        if isinstance(value, str) and len(value) != 10:
            raise ValueError("dates must be ISO calendar dates without times")
        return value

    @model_validator(mode="after")
    def validate_date_window(self) -> PaymentQuery:
        days = (self.date_to - self.date_from).days
        if days < 0:
            raise ValueError("date_to must be on or after date_from")
        if days > 30:
            raise ValueError("date window must contain at most 31 inclusive days")
        return self


class PaymentStatus(StrEnum):
    POSTED = "POSTED"
    PENDING = "PENDING"
    REVERSED = "REVERSED"


class PaymentSource(StrEnum):
    HISTORY = "history"
    PENDING = "pending"


class PaymentDirection(StrEnum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class PaymentRecord(ContractModel):
    member_id: Annotated[str, StringConstraints(pattern=r"^\d{5}$")]
    account_id: Identifier
    reference: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    direction: PaymentDirection
    amount: Money
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    transaction_date: date
    status: PaymentStatus
    source: PaymentSource
    observation_id: Identifier

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> object:
        return _validate_positive_money(value)  # type: ignore[arg-type]


class SearchCoverage(ContractModel):
    accounts_complete: bool
    sources_complete: bool
    pages_complete: bool
    accounts_searched: int = Field(ge=0)
    pages_searched: int = Field(ge=0)

    @computed_field
    @property
    def complete(self) -> bool:
        return self.accounts_complete and self.sources_complete and self.pages_complete


class FailureCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNSUPPORTED_SURFACE = "UNSUPPORTED_SURFACE"
    POLICY_DENIED = "POLICY_DENIED"
    ACCESS_DENIED = "ACCESS_DENIED"
    LOCATOR_AMBIGUOUS = "LOCATOR_AMBIGUOUS"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    SEARCH_LIMIT_EXCEEDED = "SEARCH_LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    INCONSISTENT_RECORD = "INCONSISTENT_RECORD"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    MODEL_ERROR = "MODEL_ERROR"
    MODEL_LIMIT = "MODEL_LIMIT"
    HANDOFF_TIMEOUT = "HANDOFF_TIMEOUT"
    SESSION_LOST = "SESSION_LOST"
    CANCELLED = "CANCELLED"


class BusinessOutcomeCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    NO_ACCOUNTS = "NO_ACCOUNTS"


class CandidateSummary(ContractModel):
    account_alias: Identifier
    reference_alias: Identifier
    status: PaymentStatus


class MatchDecision(ContractModel):
    kind: Literal["success"] = "success"
    records: Annotated[list[PaymentRecord], Field(min_length=1, max_length=1)]
    coverage: SearchCoverage

    @model_validator(mode="after")
    def validate_supported_match(self) -> MatchDecision:
        if any(
            record.direction != PaymentDirection.CREDIT or record.currency != "USD"
            for record in self.records
        ):
            raise ValueError("successful matches must be incoming CREDIT/USD records")
        return self


class BusinessDecision(ContractModel):
    kind: Literal["business_outcome"] = "business_outcome"
    code: BusinessOutcomeCode
    coverage: SearchCoverage
    candidates: list[CandidateSummary] = Field(default_factory=list)


class FailureDecision(ContractModel):
    kind: Literal["failure"] = "failure"
    code: FailureCode
    coverage: SearchCoverage | None = None
    step_id: Identifier | None = None
    expected: str | None = None
    observed: str | None = None


type PaymentDecision = Annotated[
    MatchDecision | BusinessDecision | FailureDecision, Field(discriminator="kind")
]


class ResultMetadata(ContractModel):
    run_id: Identifier
    artifact_digest: Digest | None
    binding_digest: Digest | None
    evidence_refs: list[str]


class Success(ResultMetadata):
    kind: Literal["success"] = "success"
    payment: PaymentRecord
    observed_at: datetime

    @model_validator(mode="after")
    def validate_supported_payment(self) -> Success:
        if self.payment.direction != PaymentDirection.CREDIT or self.payment.currency != "USD":
            raise ValueError("success requires an incoming CREDIT/USD payment")
        return self


class BusinessOutcome(ResultMetadata):
    kind: Literal["business_outcome"] = "business_outcome"
    code: BusinessOutcomeCode
    coverage: SearchCoverage
    candidates: list[CandidateSummary] = Field(default_factory=list)


class Failure(ResultMetadata):
    kind: Literal["failure"] = "failure"
    code: FailureCode
    current_step: Identifier | None
    expected_condition: str | None
    observed_condition: str | None
    attempt_count: int = Field(ge=0)


type RunResult = Annotated[Success | BusinessOutcome | Failure, Field(discriminator="kind")]


class RunStopped(RuntimeError):
    """Internal bounded-stop signal that can be projected into a redacted Failure."""

    def __init__(
        self,
        code: FailureCode,
        step_id: str | None,
        expected: str | None,
        observed: str | None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.step_id = step_id
        self.expected = expected
        self.observed = observed


JsonLiteral = str | int | bool | None


class LiteralValue(ContractModel):
    kind: Literal["literal"] = "literal"
    value: JsonLiteral
    sensitive: Literal[False] = False


InputName = Literal["member_id", "amount", "currency", "date_from", "date_to", "reference"]


class InputValue(ContractModel):
    kind: Literal["input"] = "input"
    name: InputName


class VariableValue(ContractModel):
    kind: Literal["variable"] = "variable"
    name: Identifier
    field: Identifier | None = None


type ValueRef = Annotated[LiteralValue | InputValue | VariableValue, Field(discriminator="kind")]
INITIAL_VARIABLES = frozenset({"entry_url"})


def resolve_value(
    reference: ValueRef,
    args: PaymentQuery,
    variables: Mapping[str, object],
) -> object:
    """Resolve one explicitly tagged value; no interpolation or evaluation is supported."""

    if isinstance(reference, LiteralValue):
        return reference.value
    if isinstance(reference, InputValue):
        return getattr(args, reference.name)
    if reference.name not in variables:
        raise KeyError(reference.name)
    value = variables[reference.name]
    if reference.field is None:
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"variable {reference.name!r} is not a mapping")
    return value[reference.field]


class AccessibleRoleStrategy(ContractModel):
    kind: Literal["accessible_role"] = "accessible_role"
    role: str
    name: str | ValueRef


class VisibleTextStrategy(ContractModel):
    kind: Literal["visible_text"] = "visible_text"
    text: str | ValueRef
    exact: bool = True


class AdjacentControlStrategy(ContractModel):
    kind: Literal["adjacent_control"] = "adjacent_control"
    label: str | ValueRef
    control_role: str


class TableRelationStrategy(ContractModel):
    kind: Literal["table_relation"] = "table_relation"
    header: str
    row_value: ValueRef
    control_role: str | None = None


type TargetStrategy = Annotated[
    AccessibleRoleStrategy | VisibleTextStrategy | AdjacentControlStrategy | TableRelationStrategy,
    Field(discriminator="kind"),
]


class TargetSpec(ContractModel):
    target_id: Identifier
    surface_kind: Literal["web"]
    frame_path: list[str] = Field(default_factory=list)
    container: str | None = None
    anchor: str | None = None
    strategies: Annotated[list[TargetStrategy], Field(min_length=1)]
    cardinality: int = Field(default=1, ge=1)


class ObservedControl(ContractModel):
    target_handle: Identifier
    role: str
    label: str
    enabled: bool = True
    visible: bool = True
    filled_by_automation: bool = False
    has_value: bool = False


class Observation(ContractModel):
    observation_id: Identifier
    vendor: str | None = None
    app_version: str | None = None
    surface_features: list[str] = Field(default_factory=list)
    controls: list[ObservedControl] = Field(default_factory=list)
    state: dict[str, str | int | bool | None] = Field(default_factory=dict)


class VisiblePredicate(ContractModel):
    kind: Literal["visible"] = "visible"
    target_id: Identifier


class AbsentPredicate(ContractModel):
    kind: Literal["absent"] = "absent"
    target_id: Identifier


class EqualsPredicate(ContractModel):
    kind: Literal["equals"] = "equals"
    left: ValueRef
    right: ValueRef


class CountOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"


class CountPredicate(ContractModel):
    kind: Literal["count"] = "count"
    target_id: Identifier
    operator: CountOperator
    expected: int = Field(ge=0)


class AllPredicate(ContractModel):
    kind: Literal["all"] = "all"
    predicates: Annotated[list[Predicate], Field(min_length=1)]


class AnyPredicate(ContractModel):
    kind: Literal["any"] = "any"
    predicates: Annotated[list[Predicate], Field(min_length=1)]


type Predicate = Annotated[
    VisiblePredicate
    | AbsentPredicate
    | EqualsPredicate
    | CountPredicate
    | AllPredicate
    | AnyPredicate,
    Field(discriminator="kind"),
]


class Provenance(ContractModel):
    kind: Literal["observed", "generalized", "authored"]
    event_ids: list[Identifier] = Field(default_factory=list)
    validation_scenario: Identifier | None = None

    @model_validator(mode="after")
    def validate_origin(self) -> Provenance:
        if self.kind == "observed" and not self.event_ids:
            raise ValueError("observed provenance requires at least one event ID")
        if self.kind != "observed" and self.validation_scenario is None:
            raise ValueError("generalized and authored provenance require a validation scenario")
        return self


class CheckpointRole(StrEnum):
    MEMBER_IDENTITY_VERIFIED = "member_identity_verified"
    ACCOUNT_IDENTITY_VERIFIED = "account_identity_verified"
    SOURCE_IDENTITY_VERIFIED = "source_identity_verified"
    FILTERS_VERIFIED = "filters_verified"
    PAGE_EXHAUSTED = "page_exhausted"
    SOURCE_EXHAUSTED = "source_exhausted"
    ACCOUNTS_EXHAUSTED = "accounts_exhausted"
    PAYMENT_IDENTITY_VERIFIED = "payment_identity_verified"


class NavigateStep(ContractModel):
    kind: Literal["navigate"] = "navigate"
    step_id: Identifier
    url: ValueRef
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance


class FillStep(ContractModel):
    kind: Literal["fill"] = "fill"
    step_id: Identifier
    target_id: Identifier
    value: ValueRef
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance


class ClickStep(ContractModel):
    kind: Literal["click"] = "click"
    step_id: Identifier
    target_id: Identifier
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance


class ReadParser(StrEnum):
    TEXT = "text"
    EXACT_MONEY = "exact_money"
    DATE = "date"
    ENUM = "enum"
    TABLE_ROWS = "table_rows"
    FIELDS = "fields"


class ReadRole(StrEnum):
    ACCOUNTS = "accounts"
    PAYMENT_ROWS = "payment_rows"
    PAYMENT_DETAIL = "payment_detail"


class ReadStep(ContractModel):
    kind: Literal["read"] = "read"
    step_id: Identifier
    target_id: Identifier
    parser: ReadParser
    read_role: ReadRole | None = None
    account: ValueRef | None = None
    source: ValueRef | None = None
    store_as: Identifier
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def validate_read_role(self) -> ReadStep:
        if self.read_role == ReadRole.PAYMENT_ROWS:
            if self.parser != ReadParser.TABLE_ROWS or self.account is None or self.source is None:
                raise ValueError(
                    "payment_rows read requires table_rows parser, account, and source"
                )
        elif self.account is not None or self.source is not None:
            raise ValueError("account and source context are only valid for payment_rows reads")
        if self.read_role == ReadRole.ACCOUNTS and self.parser != ReadParser.TABLE_ROWS:
            raise ValueError("accounts read requires table_rows parser")
        if self.read_role == ReadRole.PAYMENT_DETAIL and self.parser != ReadParser.FIELDS:
            raise ValueError("payment_detail read requires fields parser")
        return self


class AssertStep(ContractModel):
    kind: Literal["assert"] = "assert"
    step_id: Identifier
    predicate: Predicate
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance


class BranchCase(ContractModel):
    guard: Predicate
    steps: list[Step]


class BranchStep(ContractModel):
    kind: Literal["branch"] = "branch"
    step_id: Identifier
    cases: Annotated[list[BranchCase], Field(min_length=1)]
    fallback_failure: FailureCode | None = None
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def validate_guards(self) -> BranchStep:
        guards = [case.guard.model_dump_json() for case in self.cases]
        if len(guards) != len(set(guards)):
            raise ValueError("branch guards must be disjoint; duplicate guards found")
        if self.fallback_failure is None:
            raise ValueError("branch requires an explicit fallback failure")
        return self


class ForEachStep(ContractModel):
    kind: Literal["for_each"] = "for_each"
    step_id: Identifier
    collection: ValueRef
    item_variable: Identifier
    max_items: int = Field(ge=1, le=10)
    steps: list[Step]
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance


class PaginateStep(ContractModel):
    kind: Literal["paginate"] = "paginate"
    step_id: Identifier
    next_target_id: Identifier
    until: Predicate
    max_pages: int = Field(ge=1, le=3)
    steps: list[Step]
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance


class PaymentFieldBindings(ContractModel):
    member_id: VariableValue
    account_id: VariableValue
    reference: VariableValue
    direction: VariableValue
    amount: VariableValue
    currency: VariableValue
    transaction_date: VariableValue
    status: VariableValue
    source: VariableValue
    observation_id: VariableValue

    @model_validator(mode="after")
    def require_matching_fields(self) -> PaymentFieldBindings:
        for field_name in type(self).model_fields:
            reference = getattr(self, field_name)
            if reference.field != field_name:
                raise ValueError(f"{field_name} must select the matching payment detail field")
        return self


class PaymentDecisionBindings(ContractModel):
    """Explicit reduction of observed reads after bounded traversal completes."""

    kind: Literal["payment_decision"] = "payment_decision"
    record_steps: Annotated[list[Identifier], Field(min_length=1)]
    detail_steps: Annotated[list[Identifier], Field(min_length=1)]

    @field_validator("record_steps", "detail_steps")
    @classmethod
    def unique_reads(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("payment decision requires unique read step IDs")
        return value


class ReturnStep(ContractModel):
    kind: Literal["return"] = "return"
    step_id: Identifier
    result_kind: Literal["success", "business_outcome", "failure", "payment_decision"]
    result: PaymentDecisionBindings | PaymentFieldBindings | ValueRef
    checks: list[Predicate]
    checkpoint: Identifier | None
    checkpoint_role: CheckpointRole | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def validate_static_return_shape(self) -> ReturnStep:
        if self.result_kind == "payment_decision":
            if not isinstance(self.result, PaymentDecisionBindings):
                raise ValueError("payment_decision return requires PaymentDecisionBindings")
            return self
        if self.result_kind == "success":
            if not isinstance(self.result, PaymentFieldBindings):
                raise ValueError("success return requires PaymentFieldBindings")
            return self
        if not isinstance(self.result, LiteralValue):
            raise ValueError("business and failure returns require a literal result code")
        value = self.result.value
        if self.result_kind == "business_outcome" and value not in {
            item.value for item in BusinessOutcomeCode
        }:
            raise ValueError("business outcome return must contain a valid outcome code")
        if self.result_kind == "failure" and value not in {item.value for item in FailureCode}:
            raise ValueError("failure return must contain a valid failure code")
        return self


type Step = Annotated[
    NavigateStep
    | FillStep
    | ClickStep
    | ReadStep
    | AssertStep
    | BranchStep
    | ForEachStep
    | PaginateStep
    | ReturnStep,
    Field(discriminator="kind"),
]


class InputContract(ContractModel):
    name: Literal["PaymentQuery"] = "PaymentQuery"
    version: Literal["1.0"] = "1.0"
    schema_ref: Literal["#/$defs/PaymentQuery"] = "#/$defs/PaymentQuery"


class OutputContract(ContractModel):
    name: Literal["RunResult"] = "RunResult"
    version: Literal["1.0"] = "1.0"
    schema_ref: Literal["#/$defs/RunResult"] = "#/$defs/RunResult"
    result_kinds: Annotated[
        list[Literal["success", "business_outcome", "failure"]], Field(min_length=1)
    ]

    @field_validator("result_kinds")
    @classmethod
    def validate_unique_kinds(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("result kinds must be unique")
        return value


class CapabilityScope(ContractModel):
    direction: Literal["CREDIT"]
    currency: Literal["USD"]
    sources: Annotated[list[Literal["history", "pending"]], Field(min_length=2, max_length=2)]
    effect: Literal["read_only"]
    max_date_window_days: Literal[31]

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        if set(value) != {"history", "pending"}:
            raise ValueError("scope must cover both history and pending")
        return value


class Handler(ContractModel):
    handler_id: Identifier
    detector: Predicate
    disposition: Literal["retry", "handoff", "fail"]
    failure_code: FailureCode
    max_attempts: int = Field(default=0, ge=0, le=2)
    backoff_ms: list[Literal[250, 1000]] = Field(default_factory=list, max_length=2)
    provenance: Provenance

    @model_validator(mode="after")
    def validate_retry(self) -> Handler:
        if self.disposition == "retry" and self.max_attempts == 0:
            raise ValueError("retry handlers require a positive attempt bound")
        if self.disposition != "retry" and (self.max_attempts or self.backoff_ms):
            raise ValueError("only retry handlers may declare attempts or backoff")
        return self


class ExecutionLimits(ContractModel):
    max_accounts: int = Field(ge=1, le=3)
    max_pages_per_source: int = Field(ge=1, le=3)
    max_rows_per_page: int = Field(ge=1, le=10)
    max_ui_actions: int = Field(ge=1, le=200)
    active_timeout_seconds: int = Field(ge=1, le=300)
    action_timeout_seconds: int = Field(ge=1, le=10)
    max_retries: int = Field(ge=0, le=2)
    retry_backoff_ms: list[Literal[250, 1000]] = Field(max_length=2)
    max_interventions: int = Field(ge=0, le=2)
    human_timeout_seconds: int = Field(ge=1, le=600)


class ScenarioResult(ContractModel):
    scenario: Identifier
    passed: bool
    evidence_refs: list[str]


class ValidationReport(ContractModel):
    artifact_digest: Digest
    binding_digest: Digest
    scenario_results: Annotated[list[ScenarioResult], Field(min_length=1)]
    validated_at: datetime


def _walk_steps(steps: Iterable[Step], depth: int = 1) -> Iterable[tuple[Step, int]]:
    for step in steps:
        yield step, depth
        if isinstance(step, BranchStep):
            for case in step.cases:
                yield from _walk_steps(case.steps, depth + 1)
        elif isinstance(step, (ForEachStep, PaginateStep)):
            yield from _walk_steps(step.steps, depth + 1)


def _walk_contracts(value: object) -> Iterable[ContractModel]:
    if isinstance(value, ContractModel):
        yield value
        for field_value in value.__dict__.values():
            yield from _walk_contracts(field_value)
    elif isinstance(value, Mapping):
        for field_value in value.values():
            yield from _walk_contracts(field_value)
    elif isinstance(value, (list, tuple)):
        for field_value in value:
            yield from _walk_contracts(field_value)


type VariableSource = (
    ReadRole | Literal["entry", "scalar", "account_item", "payment_row", "loop_item"]
)


def _validate_value_refs(
    value: object,
    available: Mapping[str, VariableSource],
    context: str,
) -> None:
    mapping_sources = {ReadRole.PAYMENT_DETAIL, "account_item", "payment_row"}
    for model in _walk_contracts(value):
        if not isinstance(model, VariableValue):
            continue
        if model.name not in available:
            raise ValueError(
                f"unknown variable {model.name!r}: referenced before it is available in {context}"
            )
        if model.field is not None and available[model.name] not in mapping_sources:
            raise ValueError(f"variable {model.name!r} does not provide named fields in {context}")


def _validate_target_use(
    target_id: str,
    available: Mapping[str, VariableSource],
    targets: Mapping[str, TargetSpec],
    *,
    mutation: bool = False,
    pagination: bool = False,
) -> None:
    target = targets.get(target_id)
    if target is None:
        raise ValueError(f"unknown target {target_id!r}")
    if mutation and target.cardinality != 1:
        label = "pagination target" if pagination else "mutation target"
        raise ValueError(f"{label} must have cardinality exactly one")
    _validate_value_refs(target.strategies, available, f"target {target_id!r}")


def _validate_predicate(
    predicate: Predicate,
    available: Mapping[str, VariableSource],
    targets: Mapping[str, TargetSpec],
) -> None:
    if isinstance(predicate, (VisiblePredicate, AbsentPredicate, CountPredicate)):
        _validate_target_use(predicate.target_id, available, targets)
    elif isinstance(predicate, EqualsPredicate):
        _validate_value_refs(predicate, available, "equals predicate")
    else:
        for child in predicate.predicates:
            _validate_predicate(child, available, targets)


def _validate_checks(
    step: Step,
    available: Mapping[str, VariableSource],
    targets: Mapping[str, TargetSpec],
) -> None:
    if step.checkpoint_role is not None:
        if step.checkpoint is None:
            raise ValueError("checkpoint_role requires a checkpoint ID")
        if not step.checks:
            raise ValueError("checkpoint_role requires an executable check")
    if isinstance(step, ReturnStep) and not step.checks:
        raise ValueError("return operation requires an executable check")
    if not step.checks and not isinstance(step, (AssertStep, BranchStep, PaginateStep)):
        raise ValueError(f"step {step.step_id!r} requires an executable check")
    for predicate in step.checks:
        _validate_predicate(predicate, available, targets)


def _joined_variables(
    branches: list[dict[str, VariableSource]],
) -> dict[str, VariableSource]:
    if not branches:
        return {}
    common_names = set.intersection(*(set(branch) for branch in branches))
    return {
        name: branches[0][name]
        for name in common_names
        if all(branch[name] == branches[0][name] for branch in branches[1:])
    }


def _validate_sequence(
    steps: Iterable[Step],
    incoming: Mapping[str, VariableSource],
    targets: Mapping[str, TargetSpec],
) -> tuple[dict[str, VariableSource], bool]:
    available = dict(incoming)
    reachable = True
    for step in steps:
        if not reachable:
            raise ValueError(f"step {step.step_id!r} is unreachable after a return")

        if isinstance(step, NavigateStep):
            _validate_value_refs(step.url, available, f"step {step.step_id!r}")
        elif isinstance(step, FillStep):
            _validate_target_use(step.target_id, available, targets, mutation=True)
            _validate_value_refs(step.value, available, f"step {step.step_id!r}")
        elif isinstance(step, ClickStep):
            _validate_target_use(step.target_id, available, targets, mutation=True)
        elif isinstance(step, ReadStep):
            _validate_target_use(step.target_id, available, targets)
            _validate_value_refs(step.account, available, f"step {step.step_id!r} account")
            _validate_value_refs(step.source, available, f"step {step.step_id!r} source")
            available[step.store_as] = step.read_role or "scalar"
        elif isinstance(step, AssertStep):
            _validate_predicate(step.predicate, available, targets)
        elif isinstance(step, BranchStep):
            continuing = []
            for case in step.cases:
                _validate_predicate(case.guard, available, targets)
                branch_variables, branch_reachable = _validate_sequence(
                    case.steps, available, targets
                )
                if branch_reachable:
                    continuing.append(branch_variables)
            if continuing:
                available = _joined_variables(continuing)
            else:
                reachable = False
        elif isinstance(step, ForEachStep):
            _validate_value_refs(step.collection, available, f"step {step.step_id!r}")
            item_source: VariableSource = "loop_item"
            if isinstance(step.collection, VariableValue):
                collection_source = available[step.collection.name]
                if collection_source == ReadRole.ACCOUNTS:
                    item_source = "account_item"
                elif collection_source == ReadRole.PAYMENT_ROWS:
                    item_source = "payment_row"
            loop_variables = available | {step.item_variable: item_source}
            _validate_sequence(step.steps, loop_variables, targets)
        elif isinstance(step, PaginateStep):
            _validate_target_use(
                step.next_target_id,
                available,
                targets,
                mutation=True,
                pagination=True,
            )
            _validate_predicate(step.until, available, targets)
            _validate_sequence(step.steps, available, targets)
        elif isinstance(step, ReturnStep):
            _validate_value_refs(step.result, available, f"step {step.step_id!r}")
            if isinstance(step.result, PaymentFieldBindings):
                sources = {available[ref.name] for ref in step.result.__dict__.values()}
                if sources != {ReadRole.PAYMENT_DETAIL}:
                    raise ValueError(
                        "PaymentFieldBindings must come from a typed payment_detail read"
                    )

        _validate_checks(step, available, targets)
        if isinstance(step, ReturnStep):
            reachable = False
    return available, reachable


class Capability(ContractModel):
    schema_version: Literal["1.0"]
    capability_id: Literal["trace_incoming_payment"]
    capability_version: Literal["0.1.0"]
    vendor: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    supported_app_versions: Annotated[list[str], Field(min_length=1)]
    required_surface_features: Annotated[list[str], Field(min_length=1)]
    input_schema: InputContract
    output_schema: OutputContract
    scope: CapabilityScope
    targets: Annotated[list[TargetSpec], Field(min_length=1)]
    steps: Annotated[list[Step], Field(min_length=1)]
    handlers: list[Handler]
    limits: ExecutionLimits
    provenance: Provenance
    validation: list[ValidationReport] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> Capability:
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("target IDs must be unique")
        targets = {target.target_id: target for target in self.targets}

        walked_steps = list(_walk_steps(self.steps))
        if any(depth > 4 for _, depth in walked_steps):
            raise ValueError("operation nesting exceeds the supported traversal structure")
        steps = [step for step, _ in walked_steps]
        step_ids = [step.step_id for step in steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step IDs must be unique")
        if not any(step.checkpoint is not None for step in steps):
            raise ValueError("artifact requires at least one checkpoint")

        declared_names = (
            INITIAL_VARIABLES
            | {item.store_as for item in steps if isinstance(item, ReadStep)}
            | {item.item_variable for item in steps if isinstance(item, ForEachStep)}
        )
        for target in self.targets:
            for model in _walk_contracts(target.strategies):
                if isinstance(model, VariableValue) and model.name not in declared_names:
                    raise ValueError(f"unknown variable {model.name!r} in target definition")

        initial_variables: dict[str, VariableSource] = {name: "entry" for name in INITIAL_VARIABLES}
        _, has_fallthrough = _validate_sequence(self.steps, initial_variables, targets)
        if has_fallthrough:
            raise ValueError("an executable artifact path ends without a typed return")
        for handler in self.handlers:
            _validate_predicate(handler.detector, initial_variables, targets)

        allowed_returns = set(self.output_schema.result_kinds)
        for step in steps:
            if not isinstance(step, ReturnStep):
                continue
            if isinstance(step.result, PaymentDecisionBindings):
                if allowed_returns != {"success", "business_outcome", "failure"}:
                    raise ValueError("payment decision requires all output result kinds")
                reads = {item.step_id: item for item in steps if isinstance(item, ReadStep)}
                for ids, role in (
                    (step.result.record_steps, ReadRole.PAYMENT_ROWS),
                    (step.result.detail_steps, ReadRole.PAYMENT_DETAIL),
                ):
                    if any(key not in reads or reads[key].read_role != role for key in ids):
                        raise ValueError(
                            "payment decision must reference correctly typed read steps"
                        )
            elif step.result_kind not in allowed_returns:
                raise ValueError(f"return kind {step.result_kind!r} is absent from output contract")
        if not any(isinstance(step, ReturnStep) for step in steps):
            raise ValueError("artifact requires a typed return operation")
        return self


class TenantBindings(ContractModel):
    tenant_id: Identifier
    vendor: str
    origin: str
    app_version: str
    timezone: Literal["America/Chicago"]
    label_aliases: dict[str, list[str]] = Field(default_factory=dict)
    frame_bindings: dict[str, list[str]] = Field(default_factory=dict)
    privacy_mappings: dict[str, str] = Field(default_factory=dict)


class PolicyRoute(ContractModel):
    path: Annotated[str, StringConstraints(pattern=r"^/")]
    methods: list[Literal["GET", "POST"]]
    effect: Literal["read_only"]


class PolicyConfig(ContractModel):
    allowed_origins: Annotated[list[str], Field(min_length=1)]
    allowed_routes: Annotated[list[PolicyRoute], Field(min_length=1)]
    allowed_control_meanings: list[str]
    denied_control_meanings: list[str]


class RunEvent(ContractModel):
    event_id: Identifier
    run_id: Identifier
    seq: int = Field(ge=0)
    timestamp: datetime
    kind: Literal[
        "run_started",
        "observation",
        "action",
        "checkpoint",
        "recovery",
        "ownership",
        "result",
    ]
    actor: Literal["automation", "system", "human_operator", "test_operator"]
    summary: Annotated[str, StringConstraints(max_length=500)]
    step_id: Identifier | None = None
    observation_id: Identifier | None = None
    provider_response_id: Identifier | None = None
    model_id: ModelIdentifier | None = None
    model_call_count: int | None = Field(default=None, ge=0)
    browser_id: Identifier | None = None
    context_id: Identifier | None = None
    page_id: Identifier | None = None
    ownership_generation: int = Field(default=0, ge=0)
    previous_owner: Literal["automation", "human_operator", "test_operator", "none"] | None = None
    current_owner: Literal["automation", "human_operator", "test_operator", "none"] | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class Intervention(ContractModel):
    intervention_id: Identifier
    run_id: Identifier
    state: Literal["waiting_human", "human", "verifying", "resumed", "aborted"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    step_id: Identifier | None
    ownership_generation: int = Field(ge=0)
    created_at: datetime

    @property
    def id(self) -> str:
        """Runtime convenience alias for controller call sites."""

        return self.intervention_id


AllPredicate.model_rebuild()
AnyPredicate.model_rebuild()
BranchCase.model_rebuild()
BranchStep.model_rebuild()
ForEachStep.model_rebuild()
PaginateStep.model_rebuild()
Capability.model_rebuild()


def contract_schema_bundle() -> dict[str, object]:
    """Return the fixed capability schema plus its resolvable call contracts."""

    roots = {
        "Capability": Capability.model_json_schema(ref_template="#/$defs/{model}"),
        "PaymentQuery": PaymentQuery.model_json_schema(ref_template="#/$defs/{model}"),
        "RunResult": TypeAdapter(RunResult).json_schema(ref_template="#/$defs/{model}"),
    }
    definitions: dict[str, object] = {}
    for root_name, schema in roots.items():
        nested = schema.pop("$defs", {})
        for name, definition in nested.items():
            existing = definitions.get(name)
            if existing is not None and existing != definition:
                raise RuntimeError(f"conflicting JSON Schema definition {name!r}")
            definitions[name] = definition
        definitions[root_name] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:casetrace:schema:1.0",
        "title": "CaseTrace capability and calling contracts",
        "$ref": "#/$defs/Capability",
        "$defs": definitions,
    }
