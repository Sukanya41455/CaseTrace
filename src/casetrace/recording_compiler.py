"""Compile a qualified Northstar discovery recording into a reusable capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .compiler import CompilationError, compile_candidate
from .contracts import (
    AccessibleRoleStrategy,
    AssertStep,
    Capability,
    CheckpointRole,
    ClickStep,
    FillStep,
    InputValue,
    LiteralValue,
    NavigateStep,
    Provenance,
    ReadParser,
    ReadRole,
    ReadStep,
    TableRelationStrategy,
    TargetSpec,
    TenantBindings,
    VariableValue,
    VisibleTextStrategy,
)
from .discovery import DiscoveryRecording, RecordedOperation


@dataclass(frozen=True)
class QualifiedTrace:
    recording: DiscoveryRecording
    navigate: RecordedOperation
    member_fill: RecordedOperation
    member_search: RecordedOperation
    account_open: RecordedOperation
    filter_fills: tuple[RecordedOperation, ...]
    apply_filters: RecordedOperation
    payment_row_reads: tuple[RecordedOperation, ...]
    pagination_clicks: tuple[RecordedOperation, ...]
    transaction_view: RecordedOperation
    detail_read: RecordedOperation
    payment_confirmation: RecordedOperation


OBSERVED_TARGET_IDS = {
    "member": "member",
    "search": "search",
    "account-open-observed": "account-open-observed",
    "amount": "amount",
    "date-from": "date-from",
    "date-to": "date-to",
    "currency": "currency",
    "direction": "direction",
    "apply": "apply",
    "history-table": "history-table",
    "next": "next",
    "history-view-observed": "history-view-observed",
    "detail": "detail",
}


def qualify_recording(
    recording: DiscoveryRecording, bindings: TenantBindings
) -> QualifiedTrace:
    """Require the complete, ordered Northstar payment-discovery success path."""

    observation = recording.recognized_surface or recording.initial_observation
    if observation.vendor != bindings.vendor or observation.app_version != bindings.app_version:
        raise CompilationError("recording vendor or app version does not match tenant bindings")

    operations = recording.operations
    navigate = _single(
        operations,
        lambda item: isinstance(item.step, NavigateStep),
        "entry navigation",
    )
    member_fill = _single(
        operations,
        lambda item: isinstance(item.step, FillStep)
        and isinstance(item.step.value, InputValue)
        and item.step.value.name == "member_id"
        and _target_name(item.target) == "Member ID",
        "member input",
    )
    member_search = _single(
        operations,
        lambda item: isinstance(item.step, ClickStep) and _target_name(item.target) == "Search",
        "member search",
    )
    account_open = _single(
        operations,
        lambda item: isinstance(item.step, ClickStep) and _target_name(item.target) == "Open",
        "account opening",
    )
    filter_fills = tuple(
        _single(
            operations,
            lambda item, name=name: isinstance(item.step, FillStep)
            and isinstance(item.step.value, InputValue)
            and item.step.value.name == name,
            f"{name} filter input",
        )
        for name in ("amount", "date_from", "date_to", "currency")
    )
    direction_fill = _single(
        operations,
        lambda item: isinstance(item.step, FillStep)
        and isinstance(item.step.value, LiteralValue)
        and item.step.value.value == "CREDIT",
        "CREDIT direction filter",
    )
    apply_filters = _single(
        operations,
        lambda item: isinstance(item.step, ClickStep)
        and _target_name(item.target) == "Apply filters",
        "filter application",
    )
    payment_row_reads = tuple(
        item
        for item in operations
        if isinstance(item.step, ReadStep)
        and item.step.read_role is ReadRole.PAYMENT_ROWS
        and item.step.parser is ReadParser.TABLE_ROWS
    )
    if not payment_row_reads:
        raise CompilationError("recording is missing payment-row reads")
    pagination_clicks = tuple(
        item
        for item in operations
        if isinstance(item.step, ClickStep) and _target_name(item.target) == "Next"
    )
    transaction_view = _single(
        operations,
        lambda item: isinstance(item.step, ClickStep) and _target_name(item.target) == "View",
        "transaction view",
    )
    detail_read = _single(
        operations,
        lambda item: isinstance(item.step, ReadStep)
        and item.step.read_role is ReadRole.PAYMENT_DETAIL
        and item.step.parser is ReadParser.FIELDS,
        "typed payment detail read",
    )
    payment_confirmation = _single(
        operations,
        lambda item: isinstance(item.step, AssertStep)
        and item.step.checkpoint_role is CheckpointRole.PAYMENT_IDENTITY_VERIFIED
        and bool(item.step.checks),
        "payment identity confirmation",
    )
    _require_order(
        operations,
        (
            navigate,
            member_fill,
            member_search,
            account_open,
            *filter_fills,
            direction_fill,
            apply_filters,
            payment_row_reads[0],
            transaction_view,
            detail_read,
            payment_confirmation,
        ),
    )
    if operations[-2:] != [detail_read, payment_confirmation]:
        raise CompilationError(
            "typed payment detail read and payment identity confirmation must finish the trace"
        )
    return QualifiedTrace(
        recording=recording,
        navigate=navigate,
        member_fill=member_fill,
        member_search=member_search,
        account_open=account_open,
        filter_fills=(*filter_fills, direction_fill),
        apply_filters=apply_filters,
        payment_row_reads=payment_row_reads,
        pagination_clicks=pagination_clicks,
        transaction_view=transaction_view,
        detail_read=detail_read,
        payment_confirmation=payment_confirmation,
    )


def build_target_catalog(trace: QualifiedTrace) -> dict[str, TargetSpec]:
    """Copy observed targets by role and add the explicit Northstar profile targets."""

    observed = {
        "member": trace.member_fill,
        "search": trace.member_search,
        "account-open-observed": trace.account_open,
        "amount": trace.filter_fills[0],
        "date-from": trace.filter_fills[1],
        "date-to": trace.filter_fills[2],
        "currency": trace.filter_fills[3],
        "direction": trace.filter_fills[4],
        "apply": trace.apply_filters,
        "history-table": trace.payment_row_reads[0],
        "history-view-observed": trace.transaction_view,
        "detail": trace.detail_read,
    }
    if any(
        not _same_target(item.target, trace.payment_row_reads[0].target)
        for item in trace.payment_row_reads[1:]
    ):
        raise CompilationError("recording has conflicting activity target semantics")
    targets = {
        role: _copy_target(operation, OBSERVED_TARGET_IDS[role])
        for role, operation in observed.items()
    }
    frame_path = trace.member_fill.target.frame_path if trace.member_fill.target else []
    if trace.pagination_clicks:
        targets["next"] = _copy_target(trace.pagination_clicks[0], OBSERVED_TARGET_IDS["next"])
    else:
        targets["next"] = TargetSpec(
            target_id=OBSERVED_TARGET_IDS["next"],
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="button", name="Next")],
        )
    targets.update(_profile_targets(frame_path))
    return targets


def compile_recording(recording: DiscoveryRecording, bindings: TenantBindings) -> Capability:
    """Build the deterministic, unvalidated capability from one qualified recording."""

    trace = qualify_recording(recording, bindings)
    targets = build_target_catalog(trace)
    return compile_candidate(recording, _capability_dict(trace, targets, bindings), bindings)


def _capability_dict(
    trace: QualifiedTrace, targets: dict[str, TargetSpec], bindings: TenantBindings
) -> dict[str, object]:
    member_missing = _visible("member-not-found")
    member_summary = _visible("member-summary")
    no_accounts = _visible("no-accounts")
    accounts_visible = _visible("accounts")
    history_steps, history_rows, history_detail = _source_steps(trace, "history")
    pending_steps, pending_rows, pending_detail = _source_steps(trace, "pending")
    generalized_member = generalized("member-not-found", trace.member_search)
    generalized_accounts = generalized("no-accounts", trace.member_search, trace.account_open)
    account_loop = generalized("multi-account", trace.account_open)
    return {
        "schema_version": "1.0",
        "capability_id": "trace_incoming_payment",
        "capability_version": "0.1.0",
        "vendor": bindings.vendor,
        "supported_app_versions": [bindings.app_version],
        "required_surface_features": trace.recording.initial_observation.surface_features,
        "input_schema": {
            "name": "PaymentQuery",
            "version": "1.0",
            "schema_ref": "#/$defs/PaymentQuery",
        },
        "output_schema": {
            "name": "RunResult",
            "version": "1.0",
            "schema_ref": "#/$defs/RunResult",
            "result_kinds": ["success", "business_outcome", "failure"],
        },
        "scope": {
            "direction": "CREDIT",
            "currency": "USD",
            "sources": ["history", "pending"],
            "effect": "read_only",
            "max_date_window_days": 31,
        },
        "targets": [target.model_dump(mode="json") for target in targets.values()],
        "steps": [
            _step(
                "navigate",
                "navigate-entry",
                observed(trace.navigate),
                url=_variable("entry_url"),
                checks=[_visible("member")],
                checkpoint="entry",
            ),
            _step(
                "fill",
                "fill-member",
                observed(trace.member_fill),
                target_id="member",
                value=_input("member_id"),
                checks=[_visible("member")],
            ),
            _step(
                "click",
                "search-member",
                observed(trace.member_search),
                target_id="search",
                checks=[_any(member_missing, member_summary)],
            ),
            _step(
                "branch",
                "member-state",
                generalized_member,
                cases=[
                    {
                        "guard": member_missing,
                        "steps": [
                            _step(
                                "return",
                                "return-member-not-found",
                                generalized_member,
                                result_kind="business_outcome",
                                result=_literal("MEMBER_NOT_FOUND"),
                                checks=[member_missing],
                            )
                        ],
                    },
                    {
                        "guard": member_summary,
                        "steps": [
                            _step(
                                "assert",
                                "member-identity-verified",
                                generalized_member,
                                predicate=member_summary,
                                checks=[member_summary],
                                checkpoint="member-identity",
                                checkpoint_role=CheckpointRole.MEMBER_IDENTITY_VERIFIED,
                            )
                        ],
                    },
                ],
                fallback_failure="UNKNOWN_STATE",
            ),
            _step(
                "read",
                "read-member-identity",
                generalized_member,
                target_id="member-summary",
                parser="fields",
                store_as="member-identity",
                checks=[member_summary],
            ),
            _step(
                "branch",
                "accounts-state",
                generalized_accounts,
                cases=[
                    {
                        "guard": no_accounts,
                        "steps": [
                            _step(
                                "return",
                                "return-no-accounts",
                                generalized_accounts,
                                result_kind="business_outcome",
                                result=_literal("NO_ACCOUNTS"),
                                checks=[no_accounts],
                            )
                        ],
                    },
                    {"guard": accounts_visible, "steps": []},
                ],
                fallback_failure="UNKNOWN_STATE",
            ),
            _step(
                "read",
                "read-accounts",
                generalized_accounts,
                target_id="accounts",
                parser="table_rows",
                read_role="accounts",
                store_as="accounts",
                checks=[accounts_visible],
            ),
            _step(
                "for_each",
                "for-each-account",
                account_loop,
                collection=_variable("accounts"),
                item_variable="account",
                max_items=3,
                checks=[accounts_visible],
                steps=[
                    _step(
                        "click",
                        "open-account",
                        account_loop,
                        target_id="account-open",
                        checks=[_visible("account-context")],
                    ),
                    _step(
                        "assert",
                        "account-identity-verified",
                        account_loop,
                        predicate=_visible("account-context"),
                        checks=[_visible("account-context")],
                        checkpoint="account-identity",
                        checkpoint_role=CheckpointRole.ACCOUNT_IDENTITY_VERIFIED,
                    ),
                    *history_steps,
                    *pending_steps,
                    _step(
                        "click",
                        "back-to-member",
                        account_loop,
                        target_id="member-back",
                        checks=[accounts_visible],
                    ),
                ],
            ),
            _step(
                "assert",
                "accounts-exhausted",
                account_loop,
                predicate=accounts_visible,
                checks=[accounts_visible],
                checkpoint="accounts-exhausted",
                checkpoint_role=CheckpointRole.ACCOUNTS_EXHAUSTED,
            ),
            _step(
                "return",
                "return-payment-decision",
                generalized("payment-decision", *trace.payment_row_reads, trace.detail_read),
                result_kind="payment_decision",
                result={
                    "kind": "payment_decision",
                    "record_steps": [history_rows, pending_rows],
                    "detail_steps": [history_detail, pending_detail],
                },
                checks=[accounts_visible],
            ),
        ],
        "handlers": [
            {
                "handler_id": "permission-denied",
                "detector": _visible("permission-denied"),
                "disposition": "fail",
                "failure_code": "ACCESS_DENIED",
                "provenance": generalized("member-not-found", trace.member_search).model_dump(
                    mode="json"
                ),
            },
            {
                "handler_id": "timeout-condition",
                "detector": _visible("timeout-condition"),
                "disposition": "fail",
                "failure_code": "TIMEOUT",
                "provenance": generalized("history-pagination", trace.apply_filters).model_dump(
                    mode="json"
                ),
            },
        ],
        "limits": {
            "max_accounts": 3,
            "max_pages_per_source": 3,
            "max_rows_per_page": 10,
            "max_ui_actions": 200,
            "active_timeout_seconds": 300,
            "action_timeout_seconds": 10,
            "max_retries": 2,
            "retry_backoff_ms": [250, 1000],
            "max_interventions": 2,
            "human_timeout_seconds": 600,
        },
        "provenance": Provenance(
            kind="observed",
            event_ids=[item.event_id for item in trace.recording.operations],
        ).model_dump(mode="json"),
        "validation": [],
    }


def _source_steps(
    trace: QualifiedTrace, source: Literal["history", "pending"]
) -> tuple[list[dict[str, object]], str, str]:
    is_history = source == "history"
    scenario = "history-pagination" if is_history else "pending-payment"
    source_provenance = generalized(scenario, trace.account_open, trace.apply_filters)
    source_target = f"{source}-tab"
    table_target = "history-table" if is_history else "pending-table"
    rows_id = f"{source}-payment-rows"
    rows_variable = f"{source}-rows"
    row_variable = f"{source}-row"
    detail_id = f"{source}-payment-detail"
    detail_variable = f"{source}-detail"
    filters = [
        ("amount", _input("amount")),
        ("date-from", _input("date_from")),
        ("date-to", _input("date_to")),
        ("currency", _input("currency")),
        ("direction", _literal("CREDIT")),
    ]
    filter_steps = [
        _step(
            "fill",
            f"{source}-fill-{target_id}",
            observed(trace.filter_fills[index]) if is_history else source_provenance,
            target_id=target_id,
            value=value,
            checks=[_visible(table_target)],
        )
        for index, (target_id, value) in enumerate(filters)
    ]
    read_provenance = (
        Provenance(
            kind="observed",
            event_ids=[item.event_id for item in trace.payment_row_reads],
        )
        if is_history
        else source_provenance
    )
    detail_provenance = observed(trace.detail_read) if is_history else source_provenance
    confirmation_provenance = (
        observed(trace.payment_confirmation) if is_history else source_provenance
    )
    page_body = [
        _step(
            "read",
            f"{source}-account-context",
            source_provenance,
            target_id="account-context",
            parser="fields",
            store_as=f"{source}-context",
            checks=[_visible("account-context")],
        ),
        _step(
            "read",
            f"{source}-applied-filters",
            source_provenance,
            target_id="filters-applied",
            parser="fields",
            store_as=f"{source}-filters",
            checks=[_visible("filters-applied")],
        ),
        _step(
            "read",
            rows_id,
            read_provenance,
            target_id=table_target,
            parser="table_rows",
            read_role="payment_rows",
            account=_variable("account", "account_id"),
            source=_literal(source),
            store_as=rows_variable,
            checks=[_visible(table_target)],
        ),
        _step(
            "for_each",
            f"for-each-{source}-row",
            source_provenance,
            collection=_variable(rows_variable),
            item_variable=row_variable,
            max_items=10,
            checks=[_visible(table_target)],
            steps=[
                _step(
                    "click",
                    f"{source}-view-payment",
                    source_provenance,
                    target_id=f"{source}-view",
                    checks=[_visible("detail")],
                ),
                _step(
                    "read",
                    detail_id,
                    detail_provenance,
                    target_id="detail",
                    parser="fields",
                    read_role="payment_detail",
                    store_as=detail_variable,
                    checks=[_visible("detail")],
                ),
                _step(
                    "assert",
                    f"{source}-payment-identity-verified",
                    confirmation_provenance,
                    predicate=_visible("detail"),
                    checks=[_visible("detail")],
                    checkpoint=f"{source}-payment-identity",
                    checkpoint_role=CheckpointRole.PAYMENT_IDENTITY_VERIFIED,
                ),
                _step(
                    "click",
                    f"{source}-back-to-activity",
                    source_provenance,
                    target_id="activity-back",
                    checks=[_visible(table_target)],
                ),
            ],
        ),
    ]
    return (
        [
            _step(
                "click",
                f"select-{source}",
                source_provenance,
                target_id=source_target,
                checks=[_visible(table_target)],
            ),
            _step(
                "assert",
                f"{source}-identity-verified",
                source_provenance,
                predicate=_visible(table_target),
                checks=[_visible(table_target)],
                checkpoint=f"{source}-identity",
                checkpoint_role=CheckpointRole.SOURCE_IDENTITY_VERIFIED,
            ),
            *filter_steps,
            _step(
                "click",
                f"{source}-apply-filters",
                observed(trace.apply_filters) if is_history else source_provenance,
                target_id="apply",
                checks=[_visible("filters-applied")],
            ),
            _step(
                "assert",
                f"{source}-filters-verified",
                source_provenance,
                predicate=_visible("filters-applied"),
                checks=[_visible("filters-applied")],
                checkpoint=f"{source}-filters",
                checkpoint_role=CheckpointRole.FILTERS_VERIFIED,
            ),
            _step(
                "paginate",
                f"paginate-{source}",
                generalized(scenario, *trace.pagination_clicks, *trace.payment_row_reads),
                next_target_id="next",
                until=_absent("next"),
                max_pages=3,
                checks=[],
                steps=page_body,
            ),
            _step(
                "assert",
                f"{source}-page-exhausted",
                source_provenance,
                predicate=_absent("next"),
                checks=[_absent("next")],
                checkpoint=f"{source}-page-exhausted",
                checkpoint_role=CheckpointRole.PAGE_EXHAUSTED,
            ),
            _step(
                "assert",
                f"{source}-exhausted",
                source_provenance,
                predicate=_absent("next"),
                checks=[_absent("next")],
                checkpoint=f"{source}-exhausted",
                checkpoint_role=CheckpointRole.SOURCE_EXHAUSTED,
            ),
        ],
        rows_id,
        detail_id,
    )


def observed(operation: RecordedOperation) -> Provenance:
    return Provenance(kind="observed", event_ids=[operation.event_id])


def generalized(scenario: str, *operations: RecordedOperation) -> Provenance:
    return Provenance(
        kind="generalized",
        event_ids=list(dict.fromkeys(item.event_id for item in operations)),
        validation_scenario=scenario,
    )


def _step(kind: str, step_id: str, provenance: Provenance, **fields: object) -> dict[str, object]:
    return {
        "kind": kind,
        "step_id": step_id,
        "checks": fields.pop("checks", []),
        "checkpoint": fields.pop("checkpoint", None),
        "provenance": provenance.model_dump(mode="json"),
        **fields,
    }


def _input(name: str) -> dict[str, str]:
    return {"kind": "input", "name": name}


def _literal(value: str) -> dict[str, str]:
    return {"kind": "literal", "value": value}


def _variable(name: str, field: str | None = None) -> dict[str, str | None]:
    return {"kind": "variable", "name": name, "field": field}


def _visible(target_id: str) -> dict[str, str]:
    return {"kind": "visible", "target_id": target_id}


def _absent(target_id: str) -> dict[str, str]:
    return {"kind": "absent", "target_id": target_id}


def _any(*predicates: dict[str, str]) -> dict[str, object]:
    return {"kind": "any", "predicates": list(predicates)}


def _copy_target(operation: RecordedOperation, target_id: str) -> TargetSpec:
    if operation.target is None:
        raise CompilationError(f"recording is missing target for {target_id}")
    return operation.target.model_copy(update={"target_id": target_id})


def _profile_targets(frame_path: list[str]) -> dict[str, TargetSpec]:
    history_reference = VariableValue(name="history-row", field="reference")
    pending_reference = VariableValue(name="pending-row", field="reference")
    account = VariableValue(name="account", field="account_id")
    return {
        "member-not-found": TargetSpec(
            target_id="member-not-found",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[VisibleTextStrategy(text="Member not found", exact=True)],
        ),
        "member-summary": TargetSpec(
            target_id="member-summary",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[VisibleTextStrategy(text="Member ID:", exact=True)],
        ),
        "accounts": TargetSpec(
            target_id="accounts",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="table", name="Deposit accounts")],
        ),
        "no-accounts": TargetSpec(
            target_id="no-accounts",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[VisibleTextStrategy(text="No deposit accounts", exact=True)],
        ),
        "account-open": TargetSpec(
            target_id="account-open",
            surface_kind="web",
            frame_path=frame_path,
            container="Deposit accounts",
            strategies=[
                TableRelationStrategy(
                    header="Account ID",
                    row_value=account,
                    control_role="link",
                )
            ],
        ),
        "history-tab": TargetSpec(
            target_id="history-tab",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="link", name="History")],
        ),
        "pending-tab": TargetSpec(
            target_id="pending-tab",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="link", name="Pending")],
        ),
        "pending-table": TargetSpec(
            target_id="pending-table",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="table", name="Pending activity")],
        ),
        "history-view": TargetSpec(
            target_id="history-view",
            surface_kind="web",
            frame_path=frame_path,
            container="History activity",
            strategies=[
                TableRelationStrategy(
                    header="Reference",
                    row_value=history_reference,
                    control_role="link",
                )
            ],
        ),
        "pending-view": TargetSpec(
            target_id="pending-view",
            surface_kind="web",
            frame_path=frame_path,
            container="Pending activity",
            strategies=[
                TableRelationStrategy(
                    header="Reference",
                    row_value=pending_reference,
                    control_role="link",
                )
            ],
        ),
        "account-context": TargetSpec(
            target_id="account-context",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="table", name="Verified account context")],
        ),
        "filters-applied": TargetSpec(
            target_id="filters-applied",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[VisibleTextStrategy(text="Applied filters", exact=False)],
        ),
        "activity-back": TargetSpec(
            target_id="activity-back",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="link", name="Back to activity")],
        ),
        "member-back": TargetSpec(
            target_id="member-back",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[AccessibleRoleStrategy(role="link", name="Back to member")],
        ),
        "permission-denied": TargetSpec(
            target_id="permission-denied",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[VisibleTextStrategy(text="Permission denied", exact=True)],
        ),
        "timeout-condition": TargetSpec(
            target_id="timeout-condition",
            surface_kind="web",
            frame_path=frame_path,
            strategies=[
                VisibleTextStrategy(
                    text="Timeout scenario: activity did not reach a normal checkpoint.",
                    exact=True,
                )
            ],
        ),
    }


def _single(
    operations: list[RecordedOperation], predicate, stage: str
) -> RecordedOperation:
    matches = [item for item in operations if predicate(item)]
    if len(matches) != 1:
        qualifier = "missing" if not matches else "ambiguous"
        raise CompilationError(f"recording has {qualifier} {stage}")
    return matches[0]


def _target_name(target: TargetSpec | None) -> str | None:
    if target is None or len(target.strategies) != 1:
        return None
    name = getattr(target.strategies[0], "name", None)
    return name if isinstance(name, str) else None


def _same_target(left: TargetSpec | None, right: TargetSpec | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.model_dump(exclude={"target_id"}) == right.model_dump(exclude={"target_id"})


def _require_order(
    operations: list[RecordedOperation], required: tuple[RecordedOperation, ...]
) -> None:
    positions = {id(item): index for index, item in enumerate(operations)}
    indexes = [positions[id(item)] for item in required]
    if indexes != sorted(indexes):
        raise CompilationError("recording stages are not in the required order")
