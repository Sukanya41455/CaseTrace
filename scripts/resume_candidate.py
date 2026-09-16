"""One-off recovery: compile a saved discovery recording with local Ollama only."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import TypeAdapter

from casetrace.compiler import compile_candidate
from casetrace.contracts import (
    Capability,
    Observation,
    PaymentQuery,
    Step,
    TargetSpec,
    TenantBindings,
)
from casetrace.discovery import DiscoveryRecording, RecordedOperation
from casetrace.integrity import capability_digest
from casetrace.policy import Policy
from casetrace.provider import OllamaProvider

_STEP_ADAPTER = TypeAdapter(Step)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_saved_recording(raw: dict[str, object], query: PaymentQuery) -> DiscoveryRecording:
    operations_raw = raw.get("operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        raise ValueError("saved recording has no recorded operations")
    surface_raw = raw.get("initial_surface")
    if not isinstance(surface_raw, dict):
        raise ValueError("saved recording has no initial surface")

    surface = Observation(
        observation_id="observation-0",
        vendor=surface_raw.get("vendor"),
        app_version=surface_raw.get("app_version"),
        surface_features=surface_raw.get("surface_features", []),
    )
    operations: list[RecordedOperation] = []
    for item in operations_raw:
        if not isinstance(item, dict):
            raise ValueError("saved recording contains an invalid operation")
        target_raw = item.get("target")
        operations.append(
            RecordedOperation(
                event_id=str(item["event_id"]),
                observation_id=str(item["observation_id"]),
                resulting_observation_id=str(item["resulting_observation_id"]),
                step=_STEP_ADAPTER.validate_python(item["step"]),
                target=TargetSpec.model_validate(target_raw) if target_raw is not None else None,
            )
        )

    sensitive_values = tuple(
        str(value)
        for name, value in query.model_dump(mode="json").items()
        if value is not None and name != "currency"
    )
    provider_calls = raw.get("provider_calls", {})
    if not isinstance(provider_calls, dict):
        raise ValueError("saved recording has invalid provider calls")
    return DiscoveryRecording(
        run_id=str(raw["run_id"]),
        goal=str(raw["goal"]),
        initial_observation=surface,
        operations=operations,
        recognized_surface=surface,
        provider_calls=provider_calls,
        sensitive_values=sensitive_values,
    )


def _load_bindings(path: Path, target: str) -> TenantBindings:
    policy = Policy.from_file(path, tenant_origin=target)
    raw = _read_json(path)["tenant_bindings"]
    if raw["origin"] == "{tenant_origin}":
        raw["origin"] = policy.tenant_origin
    bindings = TenantBindings.model_validate(raw)
    if bindings.origin != policy.tenant_origin:
        raise ValueError("binding origin differs from target")
    return bindings


def _timeout_from_env() -> float:
    raw = os.environ.get("CASETRACE_OLLAMA_TIMEOUT_SECONDS", "3600")
    try:
        timeout = float(raw)
    except ValueError as error:
        raise ValueError("CASETRACE_OLLAMA_TIMEOUT_SECONDS must be numeric") from error
    if not 0 < timeout < math.inf:
        raise ValueError("CASETRACE_OLLAMA_TIMEOUT_SECONDS must be positive and finite")
    return timeout


def canonicalize_candidate(candidate: dict[str, object]) -> dict[str, object]:
    """Repair only values already fixed by the Capability schema."""
    repaired = deepcopy(candidate)
    for field in ("input_schema", "output_schema", "scope", "limits", "provenance"):
        value = repaired.get(field)
        if isinstance(value, str):
            decoded = json.loads(value)
            if not isinstance(decoded, dict):
                raise ValueError(f"{field} must decode to an object")
            repaired[field] = decoded
    repaired["capability_id"] = "trace_incoming_payment"
    repaired["capability_version"] = "0.1.0"

    def remove_redundant_assert_target(value: object) -> None:
        if isinstance(value, dict):
            if value.get("kind") == "assert":
                value.pop("target_id", None)
            for child in value.values():
                remove_redundant_assert_target(child)
        elif isinstance(value, list):
            for child in value:
                remove_redundant_assert_target(child)

    remove_redundant_assert_target(repaired.get("steps", []))
    return repaired


def preserve_raw_candidate(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(path)
    index = 1
    while True:
        backup = path.with_name(f"candidate.raw.previous-{index}.json")
        if not backup.exists():
            path.replace(backup)
            return backup
        index += 1


async def _request_candidate(
    raw: dict[str, object],
    timeout: float,
) -> dict[str, object]:
    base_url = os.environ.get("CASETRACE_OLLAMA_URL", "") or "http://127.0.0.1:11434"
    model = os.environ.get("CASETRACE_OLLAMA_MODEL", "")
    context = int(os.environ.get("CASETRACE_OLLAMA_CONTEXT", "") or "16384")
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        provider = OllamaProvider(
            base_url=base_url,
            model=model,
            context=context,
            client=client,
        )
        decision = await provider.propose_candidate(raw, Capability.model_json_schema())
    return decision.candidate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resume only candidate generation from a saved CaseTrace recording."
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--target", default="http://127.0.0.1:8000")
    parser.add_argument("--bindings", type=Path, default=Path("config/base.json"))
    parser.add_argument("--params", type=Path, default=Path("examples/queries/posted.json"))
    parser.add_argument(
        "--check", action="store_true", help="Validate inputs without calling Ollama"
    )
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="Preserve and replace an existing raw candidate using local Ollama",
    )
    args = parser.parse_args()

    load_dotenv(Path.cwd() / ".env", override=True)
    recording_path = args.run_dir / "recording.json"
    raw_candidate_path = args.run_dir / "candidate.raw.json"
    destination = args.run_dir / "capability.json"
    raw = _read_json(recording_path)
    if not isinstance(raw, dict):
        raise ValueError("recording.json must contain an object")
    query = PaymentQuery.model_validate(_read_json(args.params))
    recording = load_saved_recording(raw, query)
    bindings = _load_bindings(args.bindings, args.target)
    timeout = _timeout_from_env()

    if args.check:
        print(
            json.dumps(
                {
                    "kind": "resume_ready",
                    "run_id": recording.run_id,
                    "operations": len(recording.operations),
                    "provider": "ollama",
                    "model": os.environ.get("CASETRACE_OLLAMA_MODEL", ""),
                    "timeout_seconds": timeout,
                }
            )
        )
        return 0
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")

    if args.regenerate and raw_candidate_path.exists():
        backup = preserve_raw_candidate(raw_candidate_path)
        print(f"Preserved the previous raw candidate at {backup}.")

    if raw_candidate_path.exists():
        candidate = _read_json(raw_candidate_path)
        if not isinstance(candidate, dict):
            raise ValueError("candidate.raw.json must contain an object")
        print("Reusing the saved raw candidate; Ollama will not be called.")
    else:
        print("Requesting the final candidate from local Ollama; browser discovery will not rerun.")
        candidate = asyncio.run(_request_candidate(raw, timeout))
        raw_temporary = raw_candidate_path.with_suffix(".json.tmp")
        raw_temporary.write_text(
            json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        raw_temporary.replace(raw_candidate_path)

    repaired = canonicalize_candidate(candidate)
    capability = compile_candidate(recording, repaired, bindings)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(capability.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    print(
        json.dumps(
            {
                "kind": "candidate",
                "validation": "required",
                "artifact": str(destination),
                "artifact_digest": capability_digest(capability),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
