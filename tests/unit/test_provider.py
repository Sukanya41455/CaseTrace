from __future__ import annotations

import json

import httpx
import pytest

from casetrace.contracts import Observation
from casetrace.provider import (
    CandidateDecision,
    FallbackProvider,
    ModelDecision,
    NavigateProposal,
    OllamaProvider,
    ProviderFailure,
    discovery_tool_declarations,
)


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
