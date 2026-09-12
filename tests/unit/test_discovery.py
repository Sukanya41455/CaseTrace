from __future__ import annotations

from types import SimpleNamespace

import pytest

from casetrace.contracts import (
    AccessibleRoleStrategy,
    FailureCode,
    Observation,
    ObservedControl,
    PaymentQuery,
    RunStopped,
    TargetSpec,
)
from casetrace.discovery import discover
from casetrace.provider import (
    ClickProposal,
    GeminiProvider,
    ModelDecision,
    NavigateProposal,
    discovery_tool_declarations,
)
from casetrace.session import SessionController


class _Models:
    def __init__(self, response: object) -> None:
        self.response = response
        self.config = None

    async def generate_content(self, **kwargs: object) -> object:
        self.config = kwargs["config"]
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.aio = SimpleNamespace(models=_Models(response))


@pytest.mark.asyncio
async def test_gemini_provider_parses_one_manual_function_call() -> None:
    response = SimpleNamespace(
        function_calls=[
            SimpleNamespace(
                name="navigate",
                args={"url_kind": "entry", "url": None, "purpose": "Open the application"},
            )
        ],
        response_id="response-1",
        model_version="gemini-3.5-flash",
    )
    client = _Client(response)
    provider = GeminiProvider(api_key="test-only-key", model="gemini-3.5-flash", client=client)

    decision = await provider.decide(
        Observation(observation_id="observation-1"),
        [{"kind": "goal", "summary": "Trace an incoming payment"}],
        discovery_tool_declarations(),
    )

    assert decision.proposal == NavigateProposal(
        url_kind="entry", url=None, purpose="Open the application"
    )
    assert decision.response_id == "response-1"
    assert decision.model_version == "gemini-3.5-flash"
    assert client.aio.models.config.automatic_function_calling.disable is True


def test_tool_schemas_are_strict_and_all_fields_are_required() -> None:
    declarations = discovery_tool_declarations()

    assert declarations
    for declaration in declarations:
        schema = declaration.parameters_json_schema
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


class _ScriptedProvider:
    def __init__(self, proposals: list[object]) -> None:
        self.proposals = list(proposals)
        self.calls = 0

    async def decide(self, observation, history, tools):
        self.calls += 1
        proposal = self.proposals.pop(0)
        return ModelDecision(
            proposal=proposal,
            response_id=f"test-response-{self.calls}",
            model_version="scripted-test-provider",
            call_index=self.calls,
        )

    async def propose_candidate(self, recording, schema):
        raise AssertionError("candidate must not be requested after a denied action")


class _PolicySurface:
    def __init__(self) -> None:
        self.denied_destination_requests = 0
        self.bound_query = None

    def bind_query(self, query: PaymentQuery) -> None:
        self.bound_query = query

    async def observe(self) -> Observation:
        return Observation(
            observation_id="observation-1",
            vendor="Northstar Synthetic Bank",
            app_version="2026.09",
            surface_features=["web"],
            controls=[
                ObservedControl(
                    target_handle="target-transfer",
                    role="button",
                    label="Transfer funds",
                )
            ],
            state={"screen": "member_summary"},
        )

    async def act(self, step, args, variables):
        if getattr(step, "target_id", None) == "target-transfer":
            raise RunStopped(
                FailureCode.POLICY_DENIED,
                step.step_id,
                "reviewed inquiry control",
                "control denied",
            )

    def target_for_handle(self, observation_id: str, handle: str) -> TargetSpec:
        assert observation_id == "observation-1"
        return TargetSpec(
            target_id=handle,
            surface_kind="web",
            strategies=[AccessibleRoleStrategy(role="button", name="Transfer funds")],
        )


class _Evidence:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_discovery_cannot_override_policy_before_control_activation() -> None:
    provider = _ScriptedProvider(
        [ClickProposal(target_handle="target-transfer", purpose="Try a financial action")]
    )
    surface = _PolicySurface()
    query = PaymentQuery.model_validate(
        {
            "member_id": "12345",
            "amount": "250.00",
            "currency": "USD",
            "date_from": "2026-09-01",
            "date_to": "2026-09-07",
        }
    )

    with pytest.raises(RunStopped) as failure:
        await discover(
            "Trace an incoming payment",
            query,
            surface,
            SessionController("test-run"),
            provider,
            _Evidence(),
            bindings=None,
        )

    assert failure.value.code is FailureCode.POLICY_DENIED
    assert surface.denied_destination_requests == 0
    assert provider.calls == 1
