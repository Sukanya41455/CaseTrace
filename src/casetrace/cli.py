"""CaseTrace command-line entry points."""

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from casetrace.contracts import (
    Capability,
    Failure,
    FailureCode,
    PaymentQuery,
    RunEvent,
    RunStopped,
    ScenarioResult,
    TenantBindings,
    ValidationReport,
    contract_schema_bundle,
)
from casetrace.evidence import EvidenceWriter
from casetrace.integrity import binding_digest, capability_digest
from casetrace.policy import Policy

app = typer.Typer(help="Discover, validate, and replay CaseTrace capabilities.")

TargetOption = Annotated[str, typer.Option("--target")]
BindingsOption = Annotated[Path, typer.Option("--bindings")]
ParamsOption = Annotated[Path, typer.Option("--params")]
OutputOption = Annotated[Path, typer.Option("--output")]


def _discovery_timeout_from_env() -> float:
    raw = os.environ.get("CASETRACE_DISCOVERY_TIMEOUT_SECONDS", "600")
    try:
        timeout = float(raw)
    except ValueError as error:
        raise ValueError("CASETRACE_DISCOVERY_TIMEOUT_SECONDS must be numeric") from error
    if not 0 < timeout < float("inf"):
        raise ValueError("CASETRACE_DISCOVERY_TIMEOUT_SECONDS must be positive and finite")
    return timeout


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _invalid() -> None:
    typer.echo(json.dumps({"kind": "failure", "code": "INVALID_INPUT"}))
    raise typer.Exit(2)


def _configuration(target: str, path: Path) -> tuple[TenantBindings, Policy]:
    policy = Policy.from_file(path, tenant_origin=target)
    raw = _json(path)["tenant_bindings"]
    if raw["origin"] == "{tenant_origin}":
        raw["origin"] = policy.tenant_origin
    bindings = TenantBindings.model_validate(raw)
    if bindings.origin != policy.tenant_origin:
        raise ValueError("binding origin differs from target")
    return bindings, policy


def _writer(output: Path, args: PaymentQuery) -> EvidenceWriter:
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be empty")
    return EvidenceWriter(
        output.parent,
        output.name,
        sensitive_values=[v for k, v in args.model_dump(mode="json").items() if k != "currency"],
    )


def _discovery_payload(capability: Capability, calls: int) -> dict[str, object]:
    return {
        "kind": "capability",
        "validation": "required",
        "model_calls": calls,
        "artifact_digest": capability_digest(capability),
        "compiler": "trusted-recording-compiler-v1",
    }


def _runtime_failure(evidence, capability, bindings) -> None:
    evidence.finish(
        Failure(
            run_id=evidence.run_id,
            artifact_digest=capability_digest(capability),
            binding_digest=binding_digest(bindings),
            evidence_refs=["events.jsonl"],
            code=FailureCode.SESSION_LOST,
            current_step=None,
            expected_condition="available browser and operator runtime",
            observed_condition="runtime stopped",
            attempt_count=0,
        )
    )


@asynccontextmanager
async def _runtime(policy, bindings, evidence, *, headed=False, operator_port=None):
    from casetrace.browser import BrowserSurface
    from casetrace.session import SessionController, SessionState

    def event(kind, summary, actor="system", **fields):
        seq = evidence.next_seq
        evidence.emit(
            RunEvent(
                event_id=f"runtime-{seq}",
                run_id=evidence.run_id,
                seq=seq,
                timestamp=datetime.now(UTC),
                kind=kind,
                actor=actor,
                summary=summary,
                browser_id=surface.browser_id,
                context_id=surface.context_id,
                page_id=surface.page_id,
                ownership_generation=session.ownership_generation,
                **fields,
            )
        )

    async def manual(metadata):
        if not metadata.get("blocked"):
            event("action", "Manual UI action; field values omitted", actor="human_operator")

    surface = await BrowserSurface.launch(
        policy, [], headless=not headed, manual_event_handler=manual
    )
    previous_owner = "automation"

    async def transition(state):
        nonlocal previous_owner
        await surface.set_human_control(state is SessionState.HUMAN)
        owner = (
            "human_operator"
            if state is SessionState.HUMAN
            else "automation"
            if state is SessionState.AUTOMATION
            else "none"
        )
        event(
            "ownership",
            f"Session state {state.value}",
            previous_owner=previous_owner,
            current_owner=owner,
        )
        previous_owner = owner
        if state is SessionState.PAUSING:
            typer.echo(
                json.dumps(
                    {"state": "waiting_human", "operator": f"http://127.0.0.1:{operator_port}"}
                ),
                err=True,
            )

    async def verify():
        return await surface.verify_resume(
            browser_id=session.browser_id,
            context_id=session.context_id,
            page_id=session.page_id,
            vendor=bindings.vendor,
            app_version=bindings.app_version,
        )

    session = SessionController(
        evidence.run_id,
        browser_id=surface.browser_id,
        context_id=surface.context_id,
        page_id=surface.page_id,
        resume_verifier=verify,
        transition_handler=transition,
    )
    server = None
    serving = None
    operator_socket = None
    try:
        if operator_port is not None:
            import uvicorn

            from casetrace.operator import OperatorDisplay, create_operator_app

            # Bind here so a busy port is a normal exception, not uvicorn's process exit.
            operator_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            operator_socket.bind(("127.0.0.1", operator_port))
            server = uvicorn.Server(
                uvicorn.Config(
                    create_operator_app(
                        session,
                        OperatorDisplay(
                            "trace_incoming_payment",
                            "Investigate the incoming payment",
                            None,
                            "none",
                        ),
                        origin=f"http://127.0.0.1:{operator_port}",
                    ),
                    host="127.0.0.1",
                    port=operator_port,
                    access_log=False,
                    log_level="error",
                )
            )

            async def serve():
                try:
                    await server.serve(sockets=[operator_socket])
                except SystemExit:
                    raise RuntimeError("operator endpoint failed") from None

            serving = asyncio.create_task(serve())
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                        raise RuntimeError("operator endpoint failed")
                    await asyncio.sleep(0.02)
        yield surface, session
    finally:
        try:
            if server is not None and serving is not None:
                server.should_exit = True
                await serving
        finally:
            if operator_socket is not None:
                operator_socket.close()
            await surface.close()


async def _execute(
    capability,
    args,
    bindings,
    policy,
    evidence,
    *,
    headed=False,
    operator_port=None,
    validation_mode=False,
):
    from casetrace.replay import replay

    async with _runtime(policy, bindings, evidence, headed=headed, operator_port=operator_port) as (
        surface,
        session,
    ):
        return await replay(
            capability, args, bindings, surface, session, evidence, validation_mode=validation_mode
        )


@app.command("replay")
def replay_command(
    artifact: Path,
    target: TargetOption,
    bindings: BindingsOption,
    params: ParamsOption,
    output: OutputOption,
    headed: bool = False,
    operator_port: Annotated[int | None, typer.Option(min=1024, max=65535)] = None,
) -> None:
    """Replay an exact validated artifact without importing or contacting a model provider."""
    try:
        args = PaymentQuery.model_validate(_json(params))
        raw_capability = _json(artifact)
        tenant, policy = _configuration(target, bindings)
        evidence = _writer(output, args)
        if isinstance(raw_capability, dict) and (
            ("schema_version" in raw_capability and raw_capability["schema_version"] != "1.0")
            or (
                "capability_version" in raw_capability
                and raw_capability["capability_version"] != "0.1.0"
            )
        ):
            evidence.finish(
                Failure(
                    run_id=evidence.run_id,
                    artifact_digest=None,
                    binding_digest=binding_digest(tenant),
                    evidence_refs=["events.jsonl"],
                    code=FailureCode.UNSUPPORTED_VERSION,
                    current_step=None,
                    expected_condition="supported artifact schema and capability version",
                    observed_condition="unsupported artifact version",
                    attempt_count=0,
                )
            )
            typer.echo(evidence.result_path.read_text(encoding="utf-8").strip())
            raise typer.Exit(1)
        capability = Capability.model_validate(raw_capability)
    except (OSError, ValueError, KeyError, TypeError):
        _invalid()
    try:
        result = asyncio.run(
            _execute(
                capability,
                args,
                tenant,
                policy,
                evidence,
                headed=headed,
                operator_port=operator_port,
            )
        )
    except Exception:
        _runtime_failure(evidence, capability, tenant)
        typer.echo(evidence.result_path.read_text(encoding="utf-8").strip())
        raise typer.Exit(1) from None
    typer.echo(evidence.result_path.read_text(encoding="utf-8").strip())
    raise typer.Exit(1 if result.kind == "failure" else 0)


@app.command("discover")
def discover_command(
    target: TargetOption,
    bindings: BindingsOption,
    params: ParamsOption,
    output: OutputOption,
    goal: Annotated[str, typer.Option()],
    headed: bool = False,
) -> None:
    """Discover through real provider UI tool calls and save an unvalidated capability."""
    try:
        args = PaymentQuery.model_validate(_json(params))
        tenant, policy = _configuration(target, bindings)
        evidence = _writer(output, args)
        timeout_seconds = _discovery_timeout_from_env()
    except (OSError, ValueError, KeyError, TypeError):
        _invalid()

    async def run():
        from casetrace.discovery import discover
        from casetrace.provider import provider_from_env

        provider = provider_from_env()
        async with _runtime(policy, tenant, evidence, headed=headed) as (surface, session):
            capability = await discover(
                goal,
                args,
                surface,
                session,
                provider,
                evidence,
                bindings=tenant,
                entry_url=target.rstrip("/") + "/",
                timeout_seconds=timeout_seconds,
            )
            _write_json(evidence.run_dir / "capability.json", capability.model_dump(mode="json"))
            await evidence.capture(surface)
            evidence.seal(
                capability_digest(capability),
                binding_digest(tenant),
                mode="discovery",
            )
            return capability, provider.calls

    try:
        capability, calls = asyncio.run(run())
    except Exception as error:
        observed_condition = (
            f"{type(error).__name__}: {error}"
            if isinstance(error, ValueError)
            else f"{type(error).__name__} during discovery"
        )
        result = Failure(
            run_id=evidence.run_id,
            artifact_digest=None,
            binding_digest=binding_digest(tenant),
            evidence_refs=["events.jsonl"],
            code=error.code if isinstance(error, RunStopped) else FailureCode.MODEL_ERROR,
            current_step=error.step_id if isinstance(error, RunStopped) else None,
            expected_condition="genuine UI discovery and valid compiled capability",
            observed_condition=(
                error.observed if isinstance(error, RunStopped) else observed_condition
            ),
            attempt_count=0,
        )
        evidence.finish(result, mode="discovery")
        typer.echo(evidence.result_path.read_text(encoding="utf-8").strip())
        raise typer.Exit(1) from None
    typer.echo(json.dumps(_discovery_payload(capability, calls)))


class ValidationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    params: PaymentQuery
    expected_kind: str
    expected_code: str | None = None
    expected_status: str | None = None


@app.command("validate-artifact")
def validate_artifact(
    artifact: Path,
    target: TargetOption,
    bindings: BindingsOption,
    cases: Annotated[Path, typer.Option()],
    output: OutputOption,
) -> None:
    """Execute every named case against the already-running fixture and seal exact digests."""
    try:
        capability = Capability.model_validate(_json(artifact))
        tenant, policy = _configuration(target, bindings)
        case_list = [ValidationCase.model_validate(item) for item in _json(cases)]
        if not case_list or output.exists():
            raise ValueError("cases required and output must be new")
        output.mkdir(parents=True)
    except (OSError, ValueError, KeyError, TypeError):
        _invalid()

    async def run():
        results = []
        for index, case in enumerate(case_list):
            evidence = _writer(output / f"case-{index}", case.params)
            try:
                result = await _execute(
                    capability, case.params, tenant, policy, evidence, validation_mode=True
                )
            except Exception:
                _runtime_failure(evidence, capability, tenant)
                raise
            passed = result.kind == case.expected_kind
            if case.expected_code is not None:
                passed &= getattr(result, "code", None) == case.expected_code
            if case.expected_status is not None:
                passed &= (
                    getattr(getattr(result, "payment", None), "status", None)
                    == case.expected_status
                )
            results.append(
                ScenarioResult(
                    scenario=case.scenario,
                    passed=passed,
                    evidence_refs=[f"case-{index}/manifest.json"],
                )
            )
        return ValidationReport(
            artifact_digest=capability_digest(capability),
            binding_digest=binding_digest(tenant),
            scenario_results=results,
            validated_at=datetime.now(UTC),
        )

    try:
        report = asyncio.run(run())
    except Exception:
        typer.echo(json.dumps({"kind": "failure", "code": "SESSION_LOST"}))
        raise typer.Exit(1) from None
    _write_json(output / "validation.json", report.model_dump(mode="json"))
    passed = all(case.passed for case in report.scenario_results)
    if passed:
        validated = capability.model_copy(update={"validation": [*capability.validation, report]})
        _write_json(output / "capability.json", validated.model_dump(mode="json"))
    typer.echo(json.dumps({"passed": passed, "artifact_digest": report.artifact_digest}))
    raise typer.Exit(0 if passed else 1)


@app.command()
def doctor(live_model: bool = False) -> None:
    """Check the local runtime; contact the configured provider only with --live-model."""
    result = {
        "python": sys.version.split()[0],
        "isolated": sys.prefix != sys.base_prefix,
        "playwright": version("playwright"),
        "model_checked": False,
    }
    if live_model:
        from casetrace.provider import (
            ProviderFailure,
            discovery_tool_declarations,
            provider_from_env,
        )

        async def probe():
            from casetrace.contracts import Observation

            provider = provider_from_env()
            return await provider.decide(
                Observation(observation_id="doctor", controls=[], state={}),
                [{"kind": "goal", "summary": "Connectivity check: request navigation to entry."}],
                discovery_tool_declarations(),
            )

        try:
            decision = asyncio.run(probe())
        except ProviderFailure as error:
            typer.echo(
                json.dumps({"kind": "failure", "code": "MODEL_ERROR", "category": error.category})
            )
            raise typer.Exit(1) from None
        except Exception:
            typer.echo(json.dumps({"kind": "failure", "code": "MODEL_ERROR"}))
            raise typer.Exit(1) from None
        result.update(
            model_checked=True,
            model=decision.model_version,
            response_id=decision.response_id,
            model_calls=decision.call_index,
        )
    typer.echo(json.dumps(result))


@app.command("evidence-check")
def evidence_check(root: Path) -> None:
    """Verify linked evidence and reject incomplete delivery evidence."""
    from casetrace.evidence import check_evidence

    problems = check_evidence(root)
    typer.echo(json.dumps({"passed": not problems, "problems": problems}))
    raise typer.Exit(1 if problems else 0)


@app.callback()
def main() -> None:
    """Run a CaseTrace command."""

    load_dotenv(Path.cwd() / ".env", override=False)


@app.command("schema")
def export_schema(
    output: Annotated[Path, typer.Option("--output", help="Destination JSON Schema file.")],
) -> None:
    """Export the versioned capability JSON Schema."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(contract_schema_bundle(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    typer.echo(str(output))


@app.command()
def fixture(
    scenario: Annotated[
        str, typer.Option("--scenario", help="Deterministic fixture scenario.")
    ] = "normal",
    port: Annotated[int, typer.Option("--port", help="Loopback server port.")] = 8000,
) -> None:
    """Run the isolated synthetic bank fixture."""

    from casetrace.fixture.app import SCENARIOS, run_fixture

    if scenario not in SCENARIOS:
        raise typer.BadParameter(
            f"scenario must be one of: {', '.join(SCENARIOS)}",
            param_hint="--scenario",
        )
    run_fixture(scenario, port)


if __name__ == "__main__":
    app()
