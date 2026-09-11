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
    computed_field,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[\w.-]+$")]
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
        if isinstance(value, datetime) or (isinstance(value, str) and len(value) != 10):
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


type ValueRef = Annotated[
    LiteralValue | InputValue | VariableValue, Field(discriminator="kind")
]
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
    AccessibleRoleStrategy
    | VisibleTextStrategy
    | AdjacentControlStrategy
    | TableRelationStrategy,
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


class NavigateStep(ContractModel):
    kind: Literal["navigate"] = "navigate"
    step_id: Identifier
    url: ValueRef
    checks: list[Predicate]
    checkpoint: Identifier | None
    provenance: Provenance


class FillStep(ContractModel):
    kind: Literal["fill"] = "fill"
    step_id: Identifier
    target_id: Identifier
    value: ValueRef
    checks: list[Predicate]
    checkpoint: Identifier | None
    provenance: Provenance


class ClickStep(ContractModel):
    kind: Literal["click"] = "click"
    step_id: Identifier
    target_id: Identifier
    checks: list[Predicate]
    checkpoint: Identifier | None
    provenance: Provenance


class ReadParser(StrEnum):
    TEXT = "text"
    EXACT_MONEY = "exact_money"
    DATE = "date"
    ENUM = "enum"
    TABLE_ROWS = "table_rows"


class ReadStep(ContractModel):
    kind: Literal["read"] = "read"
    step_id: Identifier
    target_id: Identifier
    parser: ReadParser
    store_as: Identifier
    checks: list[Predicate]
    checkpoint: Identifier | None
    provenance: Provenance


class AssertStep(ContractModel):
    kind: Literal["assert"] = "assert"
    step_id: Identifier
    predicate: Predicate
    checks: list[Predicate]
    checkpoint: Identifier | None
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
    provenance: Provenance


class ReturnStep(ContractModel):
    kind: Literal["return"] = "return"
    step_id: Identifier
    result_kind: Literal["success", "business_outcome", "failure"]
    result: ValueRef
    checks: list[Predicate]
    checkpoint: Identifier | None
    provenance: Provenance

    @model_validator(mode="after")
    def validate_static_return_shape(self) -> ReturnStep:
        if not isinstance(self.result, LiteralValue):
            return self
        value = self.result.value
        if self.result_kind == "business_outcome" and value not in {
            item.value for item in BusinessOutcomeCode
        }:
            raise ValueError("business outcome return must contain a valid outcome code")
        if self.result_kind == "failure" and value not in {item.value for item in FailureCode}:
            raise ValueError("failure return must contain a valid failure code")
        if self.result_kind == "success":
            raise ValueError("success return must reference a verified runtime payment")
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


class OutputContract(ContractModel):
    name: Literal["RunResult"] = "RunResult"
    version: Literal["1.0"] = "1.0"
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
        known_targets = set(target_ids)

        walked_steps = list(_walk_steps(self.steps))
        if any(depth > 3 for _, depth in walked_steps):
            raise ValueError("operation nesting exceeds the supported traversal structure")
        steps = [step for step, _ in walked_steps]
        step_ids = [step.step_id for step in steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step IDs must be unique")
        if not any(step.checkpoint is not None for step in steps):
            raise ValueError("artifact requires at least one checkpoint")

        declared_variables = INITIAL_VARIABLES | {
            item.store_as for item in steps if isinstance(item, ReadStep)
        } | {item.item_variable for item in steps if isinstance(item, ForEachStep)}

        for model in _walk_contracts(self):
            if isinstance(model, (VisiblePredicate, AbsentPredicate, CountPredicate)):
                if model.target_id not in known_targets:
                    raise ValueError(f"unknown target {model.target_id!r}")
            if isinstance(model, (FillStep, ClickStep, ReadStep)):
                if model.target_id not in known_targets:
                    raise ValueError(f"unknown target {model.target_id!r}")
                target = self.targets[target_ids.index(model.target_id)]
                if isinstance(model, (FillStep, ClickStep)) and target.cardinality != 1:
                    raise ValueError("mutation targets must have cardinality exactly one")
            if isinstance(model, PaginateStep) and model.next_target_id not in known_targets:
                raise ValueError(f"unknown target {model.next_target_id!r}")
            if isinstance(model, VariableValue) and model.name not in declared_variables:
                raise ValueError(f"unknown variable {model.name!r}")

        allowed_returns = set(self.output_schema.result_kinds)
        for step in steps:
            if isinstance(step, ReturnStep) and step.result_kind not in allowed_returns:
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
    model_id: Identifier | None = None
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
