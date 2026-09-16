# CaseTrace

CaseTrace is a computer-use prototype for the included Northstar Synthetic Bank. Its example scenario finds a member's incoming payment by opening account activity, filtering transactions, and confirming the matching transaction detail. It records that flow as a typed capability and replays it deterministically with different inputs.

**Note**: CaseTrace has been tested end-to-end for windows. Linux/MacOS demo scripts and commands has been added for compatibility and has not been tested because of unavailability to linux/mac os.

## Windows PowerShell

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Ollama](https://ollama.com/) for live discovery.

### Setup

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
.\.tools\bin\uv.exe sync --locked --extra dev
.\.venv\ScriptsActivate.ps1
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.browser-cache'
.\.venv\Scripts\python.exe -m playwright install chromium
```

### Quick offline review

This path needs no model provider. It starts the synthetic bank, replays the submitted validated capability with different inputs, and writes fresh replay evidence under `runs/`.

```powershell
.\scripts\demo.ps1 -Headless
```

To use an already-running normal fixture, run:

```powershell
.\scripts\demo.ps1 -Headless -UseExistingFixture
```

The replay must finish with `kind: "success"`, payment status `"POSTED"`, and zero model calls.

### Full reproduction

This path requires Ollama because it creates a new capability through live discovery. Make sure to set up ollama locally on your device. Then copy `.env.example` to `.env`, set `CASETRACE_OLLAMA_MODEL` if you use a model other than the default, and confirm it is reachable:

```powershell
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m casetrace.cli doctor --live-model
```

Start the normal synthetic-bank fixture in one terminal and leave it running:

```powershell
.\.venv\Scripts\python.exe -m casetrace.cli fixture --scenario normal --port 8000
```

In a second terminal, run discovery, validation, and replay:

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

The final replay writes `$replayOutput\result.json` and `events.jsonl`; it must report `kind: "success"`, payment status `"POSTED"`, and zero model calls.

### Additional scenarios

The submitted evidence also includes a human handoff and a hard checkpoint failure. To reproduce the handoff, start this fixture in terminal A:

```powershell
.\.venv\Scripts\python.exe -m casetrace.cli fixture --scenario expire-once --port 8000
```

Then run this headed replay in terminal B:

```powershell
.\.venv\Scripts\python.exe -m casetrace.cli replay evidence/validation/capability.json `
  --target http://127.0.0.1:8000 `
  --bindings config/base.json `
  --params examples/queries/posted-new-member.json `
  --output runs/review-handoff `
  --headed `
  --operator-port 8001
```

When the terminal prints `waiting_human`, open `http://127.0.0.1:8001`, take control, sign in to the already-open fixture browser with its visible fixture credentials, then resume. The same session must finish with `POSTED`; see `evidence/replay-handoff/`.

For the checkpoint-failure proof, stop the expiry fixture, start `--scenario stale-member`, then replay `examples/queries/posted.json`:

```powershell
.\.venv\Scripts\python.exe -m casetrace.cli fixture --scenario stale-member --port 8000
.\.venv\Scripts\python.exe -m casetrace.cli replay evidence/validation/capability.json `
  --target http://127.0.0.1:8000 `
  --bindings config/base.json `
  --params examples/queries/posted.json `
  --output runs/review-checkpoint-failure
```

This replay intentionally exits with `CHECKPOINT_FAILED`; see `evidence/replay-checkpoint-failure/`.

### Development checks

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
```

## Linux and macOS Bash

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Ollama](https://ollama.com/) for live discovery.

### Setup

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --extra dev
source .venv/bin/activate
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browser-cache"
.venv/bin/python -m playwright install chromium
```

### Quick offline review

This path needs no model provider. It starts the synthetic bank, replays the submitted validated capability with different inputs, and writes fresh replay evidence under `runs/`.

```bash
bash scripts/demo.sh --headless
```

To use an already-running normal fixture, run:

```bash
bash scripts/demo.sh --headless --use-existing-fixture
```

The replay must finish with `kind: "success"`, payment status `"POSTED"`, and zero model calls.

### Full reproduction

This path requires Ollama because it creates a new capability through live discovery. Copy `.env.example` to `.env`, set `CASETRACE_OLLAMA_MODEL` if you use a model other than the default, and confirm it is reachable:

```bash
cp .env.example .env
.venv/bin/python -m casetrace.cli doctor --live-model
```

Start the normal synthetic-bank fixture in one terminal and leave it running:

```bash
.venv/bin/python -m casetrace.cli fixture --scenario normal --port 8000
```

In a second terminal, run discovery, validation, and replay:

```bash
run_root="runs/review-$(date +%Y%m%d-%H%M%S)"
discovery_output="$run_root/discovery"
validation_output="$run_root/validation"
replay_output="$run_root/replay-posted"

.venv/bin/python -m casetrace.cli discover \
  --target http://127.0.0.1:8000 \
  --bindings config/base.json \
  --params examples/queries/posted.json \
  --output "$discovery_output" \
  --goal "Find the incoming payment described by the typed invocation, confirm its identity on the transaction detail screen, and propose the reusable bounded capability."

.venv/bin/python -m casetrace.cli validate-artifact "$discovery_output/capability.json" \
  --target http://127.0.0.1:8000 \
  --bindings config/base.json \
  --cases examples/validation/demo-cases.json \
  --output "$validation_output"

.venv/bin/python -m casetrace.cli replay "$validation_output/capability.json" \
  --target http://127.0.0.1:8000 \
  --bindings config/base.json \
  --params examples/queries/posted-new-member.json \
  --output "$replay_output"
```

The final replay writes `$replay_output/result.json` and `events.jsonl`; it must report `kind: "success"`, payment status `"POSTED"`, and zero model calls.

### Additional scenarios

The submitted evidence also includes a human handoff and a hard checkpoint failure. To reproduce the handoff, start this fixture in terminal A:

```bash
.venv/bin/python -m casetrace.cli fixture --scenario expire-once --port 8000
```

Then run this headed replay in terminal B:

```bash
.venv/bin/python -m casetrace.cli replay evidence/validation/capability.json \
  --target http://127.0.0.1:8000 \
  --bindings config/base.json \
  --params examples/queries/posted-new-member.json \
  --output runs/review-handoff \
  --headed \
  --operator-port 8001
```

When the terminal prints `waiting_human`, open `http://127.0.0.1:8001`, take control, sign in to the already-open fixture browser with its visible fixture credentials, then resume. The same session must finish with `POSTED`; see `evidence/replay-handoff/`.

For the checkpoint-failure proof, stop the expiry fixture, start `--scenario stale-member`, then replay `examples/queries/posted.json`:

```bash
.venv/bin/python -m casetrace.cli fixture --scenario stale-member --port 8000
.venv/bin/python -m casetrace.cli replay evidence/validation/capability.json \
  --target http://127.0.0.1:8000 \
  --bindings config/base.json \
  --params examples/queries/posted.json \
  --output runs/review-checkpoint-failure
```

This replay intentionally exits with `CHECKPOINT_FAILED`; see `evidence/replay-checkpoint-failure/`.

### Development checks

```bash
.venv/bin/python -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests
```

## Discovery configuration

`.env.example` documents the local Ollama settings:
- `CASETRACE_OLLAMA_URL`
- `CASETRACE_OLLAMA_MODEL`
- `CASETRACE_OLLAMA_CONTEXT`
- `CASETRACE_DISCOVERY_TIMEOUT_SECONDS` for the discovery time limit

Before discovery, verify the local model:

```powershell
ollama pull qwen3.5:9b
ollama run qwen3.5:9b "Reply with exactly: READY"
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

- The discovery run uses `examples/queries/posted.json` (`member_id` `12345`, amount `250.00`).
- The posted replay uses `examples/queries/posted-new-member.json` (`member_id` `54321`, amount `175.50`).

Keep the second file on the `replay-posted` command. Reusing `posted.json`
would make the `different_replay_inputs: true` claim in
`runs/manifest.json` unsupported by the saved instructions. The handoff replay
is a separate scenario and may continue to use the original posted query.
