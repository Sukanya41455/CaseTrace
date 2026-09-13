"""Subprocess CLI checks using authored artifacts, not model discovery evidence."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from casetrace.contracts import Capability, TenantBindings
from casetrace.integrity import binding_digest, capability_digest
from tests.harness import FixtureServer
from tests.integration.test_replay_browser import authored_traversal
from tests.unit.test_contracts import BASE


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def command(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "casetrace.cli", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def files(tmp_path, query=BASE):
    artifact = write_json(tmp_path / "authored.json", authored_traversal().model_dump(mode="json"))
    params = write_json(tmp_path / "params.json", query)
    cases = write_json(
        tmp_path / "cases.json",
        [
            {
                "scenario": "test_fixture",
                "params": query,
                "expected_kind": "success",
                "expected_status": "POSTED",
            }
        ],
    )
    return artifact, params, cases


def isolated_replay_environment(tmp_path):
    """Deny model imports and non-loopback Python egress in the actual CLI child."""
    guard = tmp_path / "isolation"
    guard.mkdir()
    log = guard / "denied.txt"
    (guard / "sitecustomize.py").write_text(
        """
import importlib.abc
import os
import sys

def deny(kind):
    with open(os.environ["CASETRACE_TEST_DENIAL_LOG"], "a") as log:
        log.write(kind + "\\n")
    raise RuntimeError("test isolation boundary")

class ModelBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ("casetrace.provider", "casetrace.discovery", "google.genai", "dotenv")
        if fullname.startswith(blocked):\n             deny("model import")
            deny("model import")

def audit(event, args):
    if event == "socket.connect" and isinstance(args[1], tuple):
        if args[1][0] not in ("127.0.0.1", "::1"):
            deny("network egress")
    if event == "socket.getaddrinfo" and args[0] not in ("127.0.0.1", "::1", "localhost"):
        deny("external DNS")

sys.meta_path.insert(0, ModelBoundary())
sys.addaudithook(audit)
""",
        encoding="utf-8",
    )
    env = os.environ | {
        "PYTHONPATH": str(guard) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "CASETRACE_TEST_DENIAL_LOG": str(log),
        "GEMINI_API_KEY": "PRIVATE-CLI-CREDENTIAL-CANARY",
        "GOOGLE_API_KEY": "PRIVATE-CLI-CREDENTIAL-CANARY",
    }
    return env, log


def test_cli_seals_executed_cases_then_replays_new_input_without_provider(tmp_path):
    artifact, params, cases = files(tmp_path)
    with FixtureServer() as fixture:
        validated_dir = tmp_path / "validated"
        validated = command(
            "validate-artifact",
            artifact,
            "--target",
            fixture.url,
            "--bindings",
            "config/base.json",
            "--cases",
            cases,
            "--output",
            validated_dir,
        )
        assert validated.returncode == 0, validated.stdout + validated.stderr
        sealed = Capability.model_validate_json((validated_dir / "capability.json").read_text())
        report = sealed.validation[-1]
        raw_bindings = json.loads(Path("config/base.json").read_text())["tenant_bindings"]
        bindings = TenantBindings.model_validate(raw_bindings | {"origin": fixture.url})
        assert report.artifact_digest == capability_digest(sealed)
        assert report.binding_digest == binding_digest(bindings)
        assert report.scenario_results[0].passed
        case_result = json.loads((validated_dir / "case-0/result.json").read_text())
        assert case_result["kind"] == "success"
        assert case_result["artifact_digest"] == report.artifact_digest
        assert case_result["payment"]["status"] == "POSTED"

        write_json(params, BASE | {"member_id": "54321", "amount": "175.50"})
        env, denied = isolated_replay_environment(tmp_path)
        replay_dir = tmp_path / "replay"
        replay = command(
            "replay",
            validated_dir / "capability.json",
            "--target",
            fixture.url,
            "--bindings",
            "config/base.json",
            "--params",
            params,
            "--output",
            replay_dir,
            env=env,
        )
        assert replay.returncode == 0, replay.stdout + replay.stderr
        result = json.loads(replay.stdout)
        assert result["kind"] == "success"
        assert result["payment"]["status"] == "POSTED"
        assert result["artifact_digest"] == report.artifact_digest
        events = [
            json.loads(line) for line in (replay_dir / "events.jsonl").read_text().splitlines()
        ]
        assert events and all(event["model_call_count"] == 0 for event in events)
        assert not denied.exists()
        assert "PRIVATE-CLI-CREDENTIAL-CANARY" not in replay.stdout + replay.stderr

        tampered = sealed.model_dump(mode="json")
        tampered["capability_version"] = "0.1.1"
        write_json(artifact, tampered)
        rejected = command(
            "replay",
            artifact,
            "--target",
            fixture.url,
            "--bindings",
            "config/base.json",
            "--params",
            params,
            "--output",
            tmp_path / "tampered",
            env=env,
        )
        assert rejected.returncode == 1
        assert json.loads(rejected.stdout)["code"] == "UNSUPPORTED_VERSION"


def test_validate_launch_failure_is_redacted_and_does_not_seal(tmp_path):
    artifact, _, cases = files(tmp_path)
    env = os.environ | {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "PRIVATE-MISSING-BROWSER")}
    output = tmp_path / "validation-failed"
    result = command(
        "validate-artifact",
        artifact,
        "--target",
        "http://127.0.0.1:8000",
        "--bindings",
        "config/base.json",
        "--cases",
        cases,
        "--output",
        output,
        env=env,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "SESSION_LOST"
    assert "PRIVATE-MISSING-BROWSER" not in result.stdout + result.stderr
    assert not (output / "capability.json").exists()
    assert json.loads((output / "case-0/result.json").read_text())["code"] == "SESSION_LOST"


def test_occupied_operator_port_returns_sanitized_failure(tmp_path):
    artifact, params, _ = files(tmp_path)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        result = command(
            "replay",
            artifact,
            "--target",
            "http://127.0.0.1:8000",
            "--bindings",
            "config/base.json",
            "--params",
            params,
            "--output",
            tmp_path / "replay",
            "--operator-port",
            occupied.getsockname()[1],
        )
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "SESSION_LOST"
    assert "Traceback" not in result.stderr
    assert "SystemExit" not in result.stderr


@pytest.mark.parametrize("operation", ["replay", "validate-artifact"])
def test_malformed_private_input_fails_preflight_with_redacted_json(tmp_path, operation):
    artifact, params, cases = files(tmp_path)
    private = params if operation == "replay" else cases
    private.write_text('{"PRIVATE-CLI-CANARY": invalid', encoding="utf-8")
    output = tmp_path / "invalid"
    result = command(
        operation,
        artifact,
        "--target",
        "http://127.0.0.1:8000",
        "--bindings",
        "config/base.json",
        "--params" if operation == "replay" else "--cases",
        private,
        "--output",
        output,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"kind": "failure", "code": "INVALID_INPUT"}
    assert "PRIVATE-CLI-CANARY" not in result.stdout + result.stderr
    assert not output.exists()
