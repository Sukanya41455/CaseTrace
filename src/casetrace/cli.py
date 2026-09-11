"""CaseTrace command-line entry points."""

import json
from pathlib import Path
from typing import Annotated

import typer

from casetrace.contracts import Capability

app = typer.Typer(help="Discover, validate, and replay CaseTrace capabilities.")


@app.callback()
def main() -> None:
    """Run a CaseTrace command."""


@app.command("schema")
def export_schema(
    output: Annotated[Path, typer.Option("--output", help="Destination JSON Schema file.")],
) -> None:
    """Export the versioned capability JSON Schema."""

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(Capability.model_json_schema(), indent=2, sort_keys=True) + "\n",
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
