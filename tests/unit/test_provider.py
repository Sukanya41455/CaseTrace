from __future__ import annotations

import asyncio
import json
from inspect import signature

import httpx
import pytest

from casetrace.contracts import Capability, Observation, ObservedControl
from casetrace.provider import (
    CandidateDecision,
    FallbackProvider,
    FillProposal,
    GeminiProvider,
    GroqProvider,
    ModelDecision,
    NavigateProposal,
    OllamaProvider,
    OpenRouterProvider,
    ProviderFailure,
    discovery_tool_declarations,
    provider_from_env,
)


def _groq_tool_response(
    name: str,
    arguments: dict[str, object],
    *,
    response_id: str = "chatcmpl-groq-1",
    model: str = "qwen/qwen3.8-27b",
) -> dict[str, object]:
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": 1789400000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-groq-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


@pytest.mark.asyncio
async def test_groq_provider_returns_required_single_typed_tool_call() -> None:
    request_body = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_groq_tool_response(
                "navigate",
                {"url_kind": "entry", "url": None, "purpose": "Open the application"},
            ),
        )

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            base_url="https://api.groq.com/openai/v1",
            client=client,
        )
        decision = await provider.decide(
            Observation(observation_id="observation-1"),
            [{"kind": "goal", "summary": "Trace an incoming payment"}],
            discovery_tool_declarations(),
        )

    assert decision == ModelDecision(
        proposal=NavigateProposal(url_kind="entry", url=None, purpose="Open the application"),
        response_id="chatcmpl-groq-1",
        model_version="qwen/qwen3.8-27b",
        call_index=1,
    )
    assert request_body["model"] == "qwen/qwen3.8-27b"
    assert request_body["tool_choice"] == "required"
    assert request_body["parallel_tool_calls"] is False
    assert request_body["temperature"] == 0
    assert request_body["tools"][0]["function"]["name"] == "navigate"


@pytest.mark.asyncio
async def test_groq_provider_marks_successful_private_reads_as_completed_progress() -> None:
    request_body = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_groq_tool_response(
                "click",
                {"target_handle": "target-1", "purpose": "Open a visible result"},
            ),
        )

    history = [
        {
            "kind": "action_result",
            "action": "read",
            "outcome": {
                "summary": "read 3 rows into private run-local state",
                "private_value_shape": {"kind": "collection", "count": 3},
            },
            "observation_id": "observation-1",
        }
    ]
    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        await provider.decide(
            Observation(observation_id="observation-1"),
            history,
            discovery_tool_declarations(),
        )

    context = json.loads(request_body["messages"][1]["content"])
    assert context["progress_contract"]["successful_action_must_advance"] is True
    assert context["progress_contract"]["private_value_shape_confirms_read"] is True


@pytest.mark.asyncio
async def test_groq_provider_returns_typed_candidate() -> None:
    request_body = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_groq_tool_response(
                "propose_candidate",
                {"schema_version": "1.0"},
                response_id="chatcmpl-groq-candidate",
            ),
        )

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            base_url="https://api.groq.com/openai/v1",
            client=client,
        )
        decision = await provider.propose_candidate(
            {"run_id": "run-discovery", "operations": []},
            {"type": "object", "properties": {"schema_version": {"type": "string"}}},
        )

    assert decision == CandidateDecision(
        candidate={"schema_version": "1.0"},
        response_id="chatcmpl-groq-candidate",
        model_version="qwen/qwen3.8-27b",
        call_index=1,
    )
    assert request_body["tool_choice"] == {
        "type": "function",
        "function": {"name": "propose_candidate"},
    }
    assert request_body["tools"][0]["function"]["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_groq_decision_retries_one_failed_tool_generation() -> None:
    attempts = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Invalid tool call generated",
                        "type": "invalid_request_error",
                        "failed_generation": "invalid arguments",
                    }
                },
            )
        return httpx.Response(
            200,
            json=_groq_tool_response(
                "navigate",
                {"url_kind": "entry", "url": None, "purpose": "Open the application"},
            ),
        )

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="openai/gpt-oss-20b",
            client=client,
        )
        decision = await provider.decide(
            Observation(observation_id="observation-1"),
            [],
            discovery_tool_declarations(),
        )

    assert decision.proposal.kind == "navigate"
    assert attempts == 2


@pytest.mark.asyncio
async def test_groq_candidate_request_compacts_schema_annotations() -> None:
    request_body = None
    request_size = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body, request_size
        request_body = json.loads(request.content)
        request_size = len(request.content)
        return httpx.Response(
            200,
            json=_groq_tool_response("propose_candidate", {"schema_version": "1.0"}),
        )

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        await provider.propose_candidate(
            {"goal": "x" * 6000},
            Capability.model_json_schema(),
        )

    parameters = request_body["tools"][0]["function"]["parameters"]

    excluded_metadata = {
        "additionalProperties",
        "default",
        "description",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "title",
    }

    def schema_metadata_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).intersection(excluded_metadata) | set().union(
                *(schema_metadata_keys(item) for item in value.values())
            )
        if isinstance(value, list):
            return set().union(*(schema_metadata_keys(item) for item in value))
        return set()

    assert schema_metadata_keys(parameters) == set()
    assert request_size < 25_000


@pytest.mark.asyncio
async def test_groq_candidate_retries_one_failed_tool_generation() -> None:
    temperatures = []

    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        temperatures.append(body["temperature"])
        if len(temperatures) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "failed_generation": {"reason": "arguments were not valid JSON"},
                    }
                },
            )
        return httpx.Response(
            200,
            json=_groq_tool_response("propose_candidate", {"schema_version": "1.0"}),
        )

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        decision = await provider.propose_candidate(
            {"run_id": "run-discovery", "operations": []},
            {"type": "object"},
        )

    assert decision.candidate == {"schema_version": "1.0"}
    assert temperatures == [0.2, 0]


@pytest.mark.asyncio
async def test_groq_candidate_stops_after_bounded_failed_generation_retry() -> None:
    attempts = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "failed_generation": {"reason": "arguments were not valid JSON"},
                }
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.propose_candidate(
                {"run_id": "run-discovery", "operations": []},
                {"type": "object"},
            )

    assert caught.value.category == "groq_tool_generation_invalid"
    assert attempts == 2


@pytest.mark.asyncio
async def test_groq_candidate_waits_for_free_tier_token_window_after_discovery(
    monkeypatch,
) -> None:
    requests = 0
    delays = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200,
                json=_groq_tool_response(
                    "navigate",
                    {"url_kind": "entry", "url": None, "purpose": "Open the application"},
                ),
            )
        return httpx.Response(
            200,
            json=_groq_tool_response("propose_candidate", {"schema_version": "1.0"}),
        )

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        await provider.decide(
            Observation(observation_id="observation-1"),
            [],
            discovery_tool_declarations(),
        )
        await provider.propose_candidate(
            {"run_id": "run-discovery", "operations": []},
            {"type": "object"},
        )

    assert delays == [60.0]


@pytest.mark.asyncio
async def test_groq_provider_rejects_multiple_tool_calls() -> None:
    response = _groq_tool_response(
        "navigate",
        {"url_kind": "entry", "url": None, "purpose": "Open the application"},
    )
    response["choices"][0]["message"]["tool_calls"].append(
        {
            "id": "call-groq-2",
            "type": "function",
            "function": {
                "name": "finish",
                "arguments": json.dumps({"purpose": "Finish"}),
            },
        }
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            base_url="https://api.groq.com/openai/v1",
            client=client,
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.decide(
                Observation(observation_id="observation-1"),
                [],
                discovery_tool_declarations(),
            )

    assert caught.value.category == "groq_tool_call_count_invalid"


@pytest.mark.asyncio
async def test_groq_provider_retries_one_rate_limit_using_retry_after(monkeypatch) -> None:
    attempts = 0
    delays = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json=_groq_tool_response(
                "navigate",
                {"url_kind": "entry", "url": None, "purpose": "Open the application"},
            ),
        )

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        decision = await provider.decide(
            Observation(observation_id="observation-1"),
            [],
            discovery_tool_declarations(),
        )

    assert decision.response_id == "chatcmpl-groq-1"
    assert attempts == 2
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_groq_provider_waits_for_reported_token_reset_before_next_request(
    monkeypatch,
) -> None:
    delays = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-ratelimit-reset-tokens": "2m3.5s"},
            json=_groq_tool_response(
                "navigate",
                {"url_kind": "entry", "url": None, "purpose": "Open the application"},
            ),
        )

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        for index in range(2):
            await provider.decide(
                Observation(observation_id=f"observation-{index}"),
                [],
                discovery_tool_declarations(),
            )

    assert delays == [123.5]


@pytest.mark.asyncio
async def test_groq_provider_stops_after_bounded_rate_limit_retry(monkeypatch) -> None:
    attempts = 0
    delays = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "341"})

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    async with httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = GroqProvider(
            api_key="test-key",
            model="qwen/qwen3.8-27b",
            client=client,
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.decide(
                Observation(observation_id="observation-1"),
                [],
                discovery_tool_declarations(),
            )

    assert caught.value.category == "transport_429"
    assert attempts == 2
    assert delays == [341.0]


def test_provider_from_env_uses_groq_when_its_key_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("CASETRACE_GROQ_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("CASETRACE_MODEL", "unused-without-key")
    monkeypatch.delenv("CASETRACE_OLLAMA_MODEL", raising=False)

    provider = provider_from_env()

    assert isinstance(provider, GroqProvider)
    assert provider.model == "qwen/qwen3.8-27b"


def test_provider_from_env_uses_openrouter_key_and_selected_free_model(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("CASETRACE_OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("CASETRACE_OLLAMA_MODEL", raising=False)

    provider = provider_from_env()

    assert isinstance(provider, OpenRouterProvider)
    assert provider.model == "nex-agi/nex-n2.5-pro:free"
    assert provider.base_url == "https://openrouter.ai/api/v1"


def test_transaction_detail_confirmation_requires_payment_identity_checkpoint() -> None:
    observation = Observation(
        observation_id="observation-detail",
        state={"screen": "transaction_detail"},
    )

    declarations = {item.name: item for item in discovery_tool_declarations(observation)}

    checkpoint_role = declarations["confirm"].parameters_json_schema["properties"][
        "checkpoint_role"
    ]
    assert checkpoint_role == {
        "type": "string",
        "enum": ["payment_identity_verified"],
    }


def test_initial_discovery_only_offers_configured_entry_navigation() -> None:
    observation = Observation(
        observation_id="observation-initial",
        vendor=None,
        state={"screen": "unknown"},
    )

    declarations = {item.name: item for item in discovery_tool_declarations(observation)}
    properties = declarations["navigate"].parameters_json_schema["properties"]

    assert properties["url_kind"] == {"type": "string", "enum": ["entry"]}
    assert properties["url"] == {"type": "null"}


def test_discovery_tools_do_not_offer_automated_filled_controls() -> None:
    assert "observation" in signature(discovery_tool_declarations).parameters
    observation = Observation(
        observation_id="observation-1",
        controls=[
            ObservedControl(
                target_handle="filled-member",
                role="textbox",
                label="Member ID",
                filled_by_automation=True,
            ),
            ObservedControl(
                target_handle="empty-amount",
                role="textbox",
                label="Amount",
            ),
            ObservedControl(
                target_handle="restored-date",
                role="textbox",
                label="Start date",
                has_value=True,
            ),
        ],
    )

    declarations = {
        declaration.name: declaration.parameters_json_schema
        for declaration in discovery_tool_declarations(observation)
    }

    assert declarations["fill_input"]["properties"]["target_handle"] == {
        "type": "string",
        "enum": ["empty-amount"],
    }

    only_filled = observation.model_copy(update={"controls": observation.controls[:1]})
    names = {item.name for item in discovery_tool_declarations(only_filled)}
    assert "fill_input" not in names
    assert "fill_literal" not in names


@pytest.mark.asyncio
async def test_ollama_rejects_tool_call_that_was_not_declared() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:9b",
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "fill_input",
                                "arguments": {
                                    "target_handle": "already-filled",
                                    "input_name": "member_id",
                                    "purpose": "Repeat a prior fill",
                                },
                            }
                        }
                    ]
                },
            },
        )

    observation = Observation(
        observation_id="observation-1",
        controls=[
            ObservedControl(
                target_handle="already-filled",
                role="textbox",
                label="Member ID",
                filled_by_automation=True,
            )
        ],
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            context=16384,
            client=client,
        )
        with pytest.raises(ProviderFailure) as caught:
            await provider.decide(
                observation,
                [],
                discovery_tool_declarations(observation),
            )

    assert caught.value.category == "ollama_tool_not_declared"


def test_gemini_rejects_handle_outside_declared_enum() -> None:
    observation = Observation(
        observation_id="observation-1",
        controls=[
            ObservedControl(
                target_handle="allowed-empty",
                role="textbox",
                label="Amount",
            )
        ],
    )

    with pytest.raises(ProviderFailure) as caught:
        GeminiProvider._parse_call(
            "fill_input",
            {
                "target_handle": "not-offered",
                "input_name": "amount",
                "purpose": "Use an invalid handle",
            },
            discovery_tool_declarations(observation),
        )

    assert caught.value.category == "gemini_tool_arguments_invalid"


@pytest.mark.asyncio
async def test_ollama_fill_input_tool_has_no_unused_literal_placeholder() -> None:
    request_body = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:9b",
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "fill_input",
                                "arguments": {
                                    "target_handle": "target-2-4",
                                    "input_name": "member_id",
                                    "purpose": "Use the symbolic member ID",
                                },
                            }
                        }
                    ]
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            context=16384,
            client=client,
        )
        decision = await provider.decide(
            Observation(observation_id="observation-1"),
            [],
            discovery_tool_declarations(),
        )

    assert decision.proposal == FillProposal(
        target_handle="target-2-4",
        value_kind="input",
        input_name="member_id",
        literal_value=None,
        purpose="Use the symbolic member ID",
    )
    declarations = {
        declaration.name: declaration.parameters_json_schema
        for declaration in discovery_tool_declarations()
    }
    assert "fill" not in declarations
    assert set(declarations["fill_input"]["properties"]) == {
        "target_handle",
        "input_name",
        "purpose",
    }
    context = json.loads(request_body["messages"][1]["content"])
    assert context["control_state_contract"] == {
        "filled_by_automation": (
            "true means the latest successful action already filled this control; "
            "do not fill it again"
        ),
        "has_value": "true means the visible control already contains a value; do not fill it",
    }


@pytest.mark.asyncio
async def test_ollama_provider_returns_typed_tool_decision() -> None:
    request_body = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:9b",
                "created_at": "2026-09-13T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "index": 0,
                                "name": "navigate",
                                "arguments": {
                                    "url_kind": "entry",
                                    "url": None,
                                    "purpose": "Open the application",
                                },
                            },
                        }
                    ],
                },
                "done": True,
                "done_reason": "stop",
                "total_duration": 1,
                "load_duration": 1,
                "prompt_eval_count": 1,
                "prompt_eval_duration": 1,
                "eval_count": 1,
                "eval_duration": 1,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            context=16384,
            client=client,
        )
        decision = await provider.decide(
            Observation(observation_id="observation-1"),
            [{"kind": "goal", "summary": "Trace an incoming payment"}],
            discovery_tool_declarations(),
        )

    assert decision == ModelDecision(
        proposal=NavigateProposal(url_kind="entry", url=None, purpose="Open the application"),
        response_id=None,
        model_version="qwen3.5:9b",
        call_index=1,
    )
    assert request_body["model"] == "qwen3.5:9b"
    assert request_body["stream"] is False
    assert request_body["think"] is False
    assert request_body["options"] == {"temperature": 0, "num_ctx": 16384}
    assert request_body["tools"][0]["function"]["name"] == "navigate"
    assert request_body["tools"][0]["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_ollama_provider_returns_typed_candidate() -> None:
    request_body = None

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.5:9b",
                "created_at": "2026-09-13T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "index": 0,
                                "name": "propose_candidate",
                                "arguments": {"schema_version": "1.0"},
                            },
                        }
                    ],
                },
                "done": True,
                "done_reason": "stop",
                "total_duration": 1,
                "load_duration": 1,
                "prompt_eval_count": 1,
                "prompt_eval_duration": 1,
                "eval_count": 1,
                "eval_duration": 1,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            context=16384,
            client=client,
        )
        decision = await provider.propose_candidate(
            {"run_id": "test-run", "operations": []},
            {"$defs": {"Step": {"$ref": "#/$defs/Step"}}},
        )

    assert decision == CandidateDecision(
        candidate={"schema_version": "1.0"},
        response_id=None,
        model_version="qwen3.5:9b",
        call_index=1,
    )
    assert request_body["tools"][0]["function"] == {
        "name": "propose_candidate",
        "description": "Return one capability candidate matching the supplied schema.",
        "parameters": {"$defs": {"Step": {"$ref": "#/$defs/Step"}}},
    }
    assert "capability_schema" not in json.loads(request_body["messages"][1]["content"])


@pytest.mark.asyncio
async def test_ollama_provider_sanitizes_http_status_category() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:11434",
        transport=httpx.MockTransport(respond),
    ) as client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            context=16384,
            client=client,
        )
        with pytest.raises(ProviderFailure) as raised:
            await provider.decide(
                Observation(observation_id="observation-1"),
                [],
                discovery_tool_declarations(),
            )

    assert raised.value.category == "transport_503"


@pytest.mark.asyncio
async def test_ollama_provider_allows_slow_local_generation(monkeypatch) -> None:
    captured_timeout = None

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "model": "qwen3.5:9b",
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "navigate",
                                "arguments": {
                                    "url_kind": "entry",
                                    "url": None,
                                    "purpose": "Open the application",
                                },
                            }
                        }
                    ]
                },
            }

    class Client:
        def __init__(self, *, base_url: str, timeout: int) -> None:
            nonlocal captured_timeout
            captured_timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, path: str, *, json: dict[str, object]) -> Response:
            return Response()

    monkeypatch.setattr("casetrace.provider.httpx.AsyncClient", Client)
    provider = OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:9b",
        context=16384,
    )

    await provider.decide(
        Observation(observation_id="observation-1"),
        [],
        discovery_tool_declarations(),
    )

    assert captured_timeout == 600


@pytest.mark.asyncio
async def test_fallback_provider_stays_local_after_retryable_primary_failure() -> None:
    class UnavailablePrimary:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, observation, history, tools):
            self.calls += 1
            raise ProviderFailure("transport_503")

        async def propose_candidate(self, recording, capability_schema):
            raise AssertionError("sticky fallback must not return to the primary")

    class WorkingBackup:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, observation, history, tools):
            self.calls += 1
            return ModelDecision(
                proposal=NavigateProposal(
                    url_kind="entry", url=None, purpose="Open the application"
                ),
                response_id=None,
                model_version="qwen3.5:9b",
                call_index=self.calls,
            )

        async def propose_candidate(self, recording, capability_schema):
            self.calls += 1
            return CandidateDecision(
                candidate={"schema_version": "1.0"},
                response_id=None,
                model_version="qwen3.5:9b",
                call_index=self.calls,
            )

    primary = UnavailablePrimary()
    backup = WorkingBackup()
    provider = FallbackProvider(primary, backup)

    decision = await provider.decide(
        Observation(observation_id="observation-1"),
        [],
        discovery_tool_declarations(),
    )
    candidate = await provider.propose_candidate({}, {})

    assert decision.model_version == "qwen3.5:9b"
    assert decision.call_index == 2
    assert candidate.model_version == "qwen3.5:9b"
    assert candidate.call_index == 3
    assert primary.calls == 1
    assert backup.calls == 2
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_fallback_provider_does_not_hide_invalid_primary_output() -> None:
    class InvalidPrimary:
        async def decide(self, observation, history, tools):
            raise ProviderFailure("tool_arguments_invalid")

        async def propose_candidate(self, recording, capability_schema):
            raise AssertionError("not used")

    class BackupMustNotRun:
        async def decide(self, observation, history, tools):
            raise AssertionError("invalid model output must remain visible")

        async def propose_candidate(self, recording, capability_schema):
            raise AssertionError("not used")

    provider = FallbackProvider(InvalidPrimary(), BackupMustNotRun())

    with pytest.raises(ProviderFailure) as caught:
        await provider.decide(
            Observation(observation_id="observation-1"),
            [],
            discovery_tool_declarations(),
        )

    assert caught.value.category == "tool_arguments_invalid"
