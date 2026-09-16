"""Command boundaries must reject private invalid input before browser/model work."""

import json
import os
import subprocess
import sys

from typer.testing import CliRunner

import casetrace.cli as cli_module
from casetrace.cli import app
from casetrace.contracts import Capability
from tests.unit.test_contracts import _artifact

runner = CliRunner()


def test_discovery_timeout_loads_from_environment(monkeypatch):
    monkeypatch.setenv("CASETRACE_DISCOVERY_TIMEOUT_SECONDS", "3600")

    assert cli_module._discovery_timeout_from_env() == 3600


def test_cli_loads_dotenv_without_overriding_existing_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "PLAYWRIGHT_BROWSERS_PATH=from-dotenv\nCASETRACE_MODEL=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("CASETRACE_MODEL", "from-shell")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "from-dotenv"
    assert os.environ["CASETRACE_MODEL"] == "from-shell"


def test_invalid_query_is_redacted_before_replay_loads_artifact(tmp_path):
    query = tmp_path / "private.json"
    query.write_text('{"member_id":"PRIVATE-CANARY"}')
    result = runner.invoke(
        app,
        [
            "replay",
            "missing.json",
            "--target",
            "http://127.0.0.1:8000",
            "--bindings",
            "config/base.json",
            "--params",
            str(query),
            "--output",
            str(tmp_path / "run"),
        ],
    )
    assert result.exit_code == 2
    assert "PRIVATE-CANARY" not in result.output
    assert json.loads(result.stdout)["code"] == "INVALID_INPUT"


def test_replay_command_imports_no_provider_when_dotenv_is_loaded(tmp_path):
    (tmp_path / ".env").write_text("GEMINI_API_KEY=test-key\n", encoding="utf-8")
    script = """
import importlib.abc, os, sys
class DenyProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('google.genai', 'casetrace.provider', 'casetrace.discovery')):
            raise RuntimeError('provider import denied')
sys.meta_path.insert(0, DenyProvider())
os.environ.pop('GEMINI_API_KEY', None)
from typer.testing import CliRunner
from casetrace.cli import app
result = CliRunner().invoke(app, [
    'replay', 'missing.json',
    '--target', 'http://127.0.0.1:8000',
    '--bindings', 'missing.json',
    '--params', 'missing.json',
    '--output', 'run',
])
assert result.exit_code == 2, result.output
assert os.environ['GEMINI_API_KEY'] == 'test-key'
print('isolated')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"


def test_evidence_check_rejects_missing_delivery_manifest(tmp_path):
    result = runner.invoke(app, ["evidence-check", str(tmp_path)])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["passed"] is False


def test_live_model_doctor_reports_only_safe_provider_category(monkeypatch):
    import casetrace.provider as provider_module
    from casetrace.provider import GeminiProvider, ProviderFailure

    class FailingProvider:
        async def decide(self, observation, history, tools):
            raise ProviderFailure("transport_503")

    monkeypatch.setattr(
        provider_module, "provider_from_env", lambda: FailingProvider(), raising=False
    )
    monkeypatch.setattr(
        GeminiProvider,
        "from_env",
        lambda: (_ for _ in ()).throw(AssertionError("provider factory was bypassed")),
    )

    result = runner.invoke(app, ["doctor", "--live-model"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "kind": "failure",
        "code": "MODEL_ERROR",
        "category": "transport_503",
    }


def test_schema_export_contains_payment_decision(tmp_path):
    output = tmp_path / "schema.json"
    result = runner.invoke(app, ["schema", "--output", str(output)])
    assert result.exit_code == 0
    assert "PaymentDecisionBindings" in output.read_text()


def test_discovery_payload_reports_compiled_capability() -> None:
    payload = cli_module._discovery_payload(Capability.model_validate(_artifact()), calls=16)

    assert payload["kind"] == "capability"
    assert payload["validation"] == "required"
    assert payload["model_calls"] == 16
    assert payload["compiler"] == "trusted-recording-compiler-v1"
    assert isinstance(payload["artifact_digest"], str)
