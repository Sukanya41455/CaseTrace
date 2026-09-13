from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from casetrace.compiler import CompilationError
from casetrace.contracts import (
    AccessibleRoleStrategy,
    CheckpointRole,
    FailureCode,
    Observation,
    ObservedControl,
    PaymentQuery,
    RunStopped,
    TargetSpec,
)
from casetrace.discovery import discover
from casetrace.evidence import EvidenceWriter
from casetrace.provider import (
    CandidateDecision,
    ClickProposal,
    ConfirmProposal,
    FinishProposal,
    GeminiProvider,
    ModelDecision,
    NavigateProposal,
    ProviderFailure,
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


async def test_provider_failure_keeps_only_safe_transport_category() -> None:
    class TransportError(Exception):
        code = 503

    class FailedModels:
        async def generate_content(self, **kwargs):
            raise TransportError("PRIVATE-KEY-CANARY")

    provider = GeminiProvider(
        api_key="test-only-key",
        model="test-model",
        client=SimpleNamespace(aio=SimpleNamespace(models=FailedModels())),
    )
    with pytest.raises(ProviderFailure) as caught:
        await provider.decide(Observation(observation_id="test"), [], discovery_tool_declarations())
    assert caught.value.category == "transport_503"
    assert "PRIVATE-KEY-CANARY" not in str(caught.value)


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


@pytest.mark.asyncio
async def test_gemini_candidate_uses_manual_call_and_returns_provider_metadata() -> None:
    response = SimpleNamespace(
        function_calls=[
            SimpleNamespace(
                name="propose_candidate",
                args={"candidate_json": '{"schema_version":"1.0"}'},
            )
        ],
        response_id="candidate-response-1",
        model_version="gemini-3.5-flash",
    )
    client = _Client(response)
    provider = GeminiProvider(api_key="test-only-key", model="gemini-3.5-flash", client=client)

    decision = await provider.propose_candidate(
        {"run_id": "test-run", "operations": []},
        {"$defs": {"Step": {"$ref": "#/$defs/Step"}}},
    )

    assert decision == CandidateDecision(
        candidate={"schema_version": "1.0"},
        response_id="candidate-response-1",
        model_version="gemini-3.5-flash",
        call_index=1,
    )
    declaration = client.aio.models.config.tools[0].function_declarations[0]
    assert declaration.name == "propose_candidate"
    assert declaration.parameters_json_schema == {
        "type": "object",
        "properties": {"candidate_json": {"type": "string"}},
        "required": ["candidate_json"],
        "additionalProperties": False,
    }
    assert client.aio.models.config.automatic_function_calling.disable is True


def test_tool_schemas_are_strict_and_all_fields_are_required() -> None:
    declarations = discovery_tool_declarations()

    assert declarations
    for declaration in declarations:
        schema = declaration.parameters_json_schema
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


class _ScriptedProvider:
    def __init__(self, proposals: list[object], candidate: dict[str, object] | None = None) -> None:
        self.proposals = list(proposals)
        self.candidate = candidate
        self.calls = 0
        self.candidate_calls = 0

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
        self.candidate_calls += 1
        if self.candidate is None:
            raise AssertionError("candidate must not be requested")
        self.calls += 1
        return CandidateDecision(
            candidate=self.candidate,
            response_id=f"test-response-{self.calls}",
            model_version="scripted-test-provider",
            call_index=self.calls,
        )


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


@pytest.mark.asyncio
async def test_discovery_cannot_override_policy_before_control_activation(tmp_path) -> None:
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
            EvidenceWriter(tmp_path, "test-run", sensitive_values=query.model_dump().values()),
            bindings=None,
        )

    assert failure.value.code is FailureCode.POLICY_DENIED
    assert surface.denied_destination_requests == 0
    assert provider.calls == 1
    recording = (tmp_path / "test-run" / "recording.json").read_text(encoding="utf-8")
    assert "12345" not in recording
    assert json.loads(recording)["operations"] == []


class _NavigationSurface:
    def __init__(self, recognized_screen: str = "member_summary") -> None:
        self.observations = 0
        self.recognized_screen = recognized_screen

    def bind_query(self, query: PaymentQuery) -> None:
        self.query = query

    async def observe(self) -> Observation:
        self.observations += 1
        if self.observations == 1:
            return Observation(observation_id="observation-blank", state={"screen": "unknown"})
        return Observation(
            observation_id=f"observation-{self.observations}",
            vendor="Northstar Synthetic Bank",
            app_version="2026.09",
            surface_features=["web", "frames", "accessible_roles"],
            state={"screen": self.recognized_screen},
        )

    async def act(self, step, args, variables) -> None:
        return None


def _query() -> PaymentQuery:
    return PaymentQuery.model_validate(
        {
            "member_id": "12345",
            "amount": "250.00",
            "currency": "USD",
            "date_from": "2026-09-01",
            "date_to": "2026-09-07",
        }
    )


@pytest.mark.asyncio
async def test_discovery_persists_each_action_and_first_recognized_surface(tmp_path) -> None:
    query = _query()
    provider = _ScriptedProvider(
        [NavigateProposal(url_kind="entry", url=None, purpose="Open member 12345")]
    )

    with pytest.raises(RunStopped) as failure:
        await discover(
            "Trace 250.00 for member 12345",
            query,
            _NavigationSurface(),
            SessionController("recording-run"),
            provider,
            EvidenceWriter(tmp_path, "recording-run", sensitive_values=query.model_dump().values()),
            bindings=None,
            entry_url="http://127.0.0.1:8000",
            max_decisions=1,
        )

    assert failure.value.code is FailureCode.MODEL_LIMIT
    persisted = json.loads(
        (tmp_path / "recording-run" / "recording.json").read_text(encoding="utf-8")
    )
    assert persisted["initial_surface"] == {
        "vendor": "Northstar Synthetic Bank",
        "app_version": "2026.09",
        "surface_features": ["web", "frames", "accessible_roles"],
    }
    assert len(persisted["operations"]) == 1
    assert "12345" not in json.dumps(persisted)
    assert "250.00" not in json.dumps(persisted)


@pytest.mark.asyncio
async def test_finish_requires_executable_confirmation_on_goal_screen(tmp_path) -> None:
    query = _query()
    provider = _ScriptedProvider(
        [
            NavigateProposal(url_kind="entry", url=None, purpose="Open the application"),
            FinishProposal(purpose="Finished"),
        ]
    )

    with pytest.raises(RunStopped) as failure:
        await discover(
            "Trace an incoming payment",
            query,
            _NavigationSurface(recognized_screen="transaction_detail"),
            SessionController("unconfirmed-run"),
            provider,
            EvidenceWriter(tmp_path, "unconfirmed-run"),
            bindings=None,
            entry_url="http://127.0.0.1:8000",
        )

    assert failure.value.code is FailureCode.CHECKPOINT_FAILED
    assert provider.candidate_calls == 0


class _ConfirmationSurface(_NavigationSurface):
    def __init__(self) -> None:
        super().__init__(recognized_screen="transaction_detail")

    async def observe(self) -> Observation:
        observation = await super().observe()
        if self.observations == 1:
            return observation
        return observation.model_copy(
            update={
                "controls": [
                    ObservedControl(
                        target_handle="payment-detail",
                        role="heading",
                        label="Payment detail",
                    )
                ]
            }
        )

    def target_for_handle(self, observation_id: str, handle: str) -> TargetSpec:
        return TargetSpec(
            target_id=handle,
            surface_kind="web",
            strategies=[AccessibleRoleStrategy(role="heading", name="Payment detail")],
        )

    async def check(self, predicate, args, variables) -> bool:
        return True


@pytest.mark.asyncio
async def test_recording_attributes_finish_and_candidate_provider_calls(tmp_path) -> None:
    query = _query()
    provider = _ScriptedProvider(
        [
            NavigateProposal(url_kind="entry", url=None, purpose="Open the application"),
            ConfirmProposal(
                target_handle="payment-detail",
                checkpoint_role=CheckpointRole.PAYMENT_IDENTITY_VERIFIED,
                purpose="Confirm payment detail",
            ),
            FinishProposal(purpose="Finish after confirmation"),
        ],
        candidate={},
    )

    with pytest.raises(CompilationError):
        await discover(
            "Trace an incoming payment",
            query,
            _ConfirmationSurface(),
            SessionController("metadata-run"),
            provider,
            EvidenceWriter(tmp_path, "metadata-run"),
            bindings=None,
            entry_url="http://127.0.0.1:8000",
        )

    persisted = json.loads(
        (tmp_path / "metadata-run" / "recording.json").read_text(encoding="utf-8")
    )
    assert persisted["provider_calls"]["finish"] == {
        "response_id": "test-response-3",
        "model_id": "scripted-test-provider",
        "call_index": 3,
    }
    assert persisted["provider_calls"]["candidate"] == {
        "response_id": "test-response-4",
        "model_id": "scripted-test-provider",
        "call_index": 4,
    }
