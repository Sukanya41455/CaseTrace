# CaseTrace

CaseTrace runs a browser-based banking workflow, records a typed capability, and replays it without calling a model.

## Quick start

Requirements:
- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

Setup:

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
.\.tools\bin\uv.exe sync --locked --extra dev
.\.venv\Scripts\Activate.ps1
```

Install the browser:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.browser-cache'
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Reviewer paths

### Quick offline review

This path needs no model provider. It starts the synthetic bank, replays the submitted validated capability with different inputs, and writes fresh replay evidence under `runs/`.

```powershell
.\scripts\demo.ps1
```

Useful demo options:

```powershell
.\scripts\demo.ps1 -Headless
.\scripts\demo.ps1 -UseExistingFixture
```

The replay must finish with `kind: "success"`, payment status `"POSTED"`, and zero model calls. The script prints the evidence directory when it finishes.

### Full reproduction

This path requires access to a configured model provider because it creates a new capability through live discovery. Copy `.env.example` to `.env`, configure one of its documented providers, and confirm it is reachable:

```powershell
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m casetrace.cli doctor --live-model
```

Start the normal synthetic-bank fixture in one terminal and leave it running:

```powershell
.\.venv\Scripts\python.exe -m casetrace.cli fixture --scenario normal --port 8000
```

In a second terminal, run discovery, validate its newly produced capability, then replay that validated artifact with different input parameters:

```powershell
$runRoot = "runs\review-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$discoveryOutput = Join-Path $runRoot 'discovery'
$validationOutput = Join-Path $runRoot 'validation'
$replayOutput = Join-Path $runRoot 'replay-posted'

.\.venv\Scripts\python.exe -m casetrace.cli discover `
  --target http://127.0.0.1:8000 `
  --bindings config/base.json `
  --params examples/queries/posted.json `
  --output $discoveryOutput `
  --goal "Find the incoming payment described by the typed invocation, confirm its identity on the transaction detail screen, and propose the reusable bounded capability."

.\.venv\Scripts\python.exe -m casetrace.cli validate-artifact "$discoveryOutput\capability.json" `
  --target http://127.0.0.1:8000 `
  --bindings config/base.json `
  --cases examples/validation/demo-cases.json `
  --output $validationOutput

.\.venv\Scripts\python.exe -m casetrace.cli replay "$validationOutput\capability.json" `
  --target http://127.0.0.1:8000 `
  --bindings config/base.json `
  --params examples/queries/posted-new-member.json `
  --output $replayOutput
```

The discovery output contains the generated `capability.json`; validation writes the reviewed capability at `$validationOutput\capability.json`; and the final replay writes `$replayOutput\result.json` and `events.jsonl`. The final replay should report `kind: "success"`, payment status `"POSTED"`, and zero model calls.

## Development checks

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
```

## Discovery configuration

`.env.example` documents the supported provider settings:
- `GEMINI_API_KEY` + `CASETRACE_MODEL` for Gemini
- `CASETRACE_OLLAMA_MODEL` for local Ollama
- `CASETRACE_DISCOVERY_TIMEOUT_SECONDS` for the discovery time limit

Before discovery, verify the local model:

```powershell
ollama pull qwen3.5:4b
ollama run qwen3.5:4b "Reply with exactly: READY"
```

## Notes

- Replay uses the project `.env` and does not call a provider.
- The fast offline demo replays the genuinely discovered and validated capability from `evidence/validation/capability.json` without calling a model.
- The submission evidence uses a genuine model-guided discovery, its compiled and validated
  capability, and zero-model replay. That discovery is required to reproduce or claim the
  completed evidence chain.
- Reviewers can inspect and replay an existing validated evidence bundle without rerunning the
  slower provider-guided discovery.

## Replay input provenance

The different-input proof uses two explicit query files:

- The discovery run uses `examples\queries\posted.json` (`member_id` `12345`, amount `250.00`).
- The posted replay uses `examples\queries\posted-new-member.json` (`member_id` `54321`, amount `175.50`).

Keep the second file on the `replay-posted` command. Reusing `posted.json`
would make the `different_replay_inputs: true` claim in
`runs\manifest.json` unsupported by the saved instructions. The handoff replay
is a separate scenario and may continue to use the original posted query.
