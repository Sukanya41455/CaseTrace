# CaseTrace

CaseTrace is being implemented as a Python system that discovers a payment-investigation workflow through a synthetic banking UI, saves a typed capability, and replays it without a model.

## Python environment

All application and development dependencies belong to the project-local `.venv`. Python 3.12 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/) are required. The dependency versions are recorded in `uv.lock`.

On this Windows workspace, a bootstrap copy of uv is available at `.tools\bin\uv.exe`:

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
.\.tools\bin\uv.exe sync --locked --extra dev
.\.venv\Scripts\Activate.ps1
```

On a fresh checkout with uv installed, use `uv sync --locked --extra dev`; uv creates and uses `.venv` automatically. Activation is optional when calling the environment's executables directly.

Install the browser through the same environment:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.browser-cache'
.\.venv\Scripts\python.exe -m playwright install chromium
```

Add `PLAYWRIGHT_BROWSERS_PATH` to the project-root `.env`; `.env.example` shows the relative project-cache value. The CaseTrace CLI automatically loads all values from that file without replacing variables already set in the shell. The one-time Playwright installer above still needs the PowerShell assignment because it runs Playwright directly rather than through the CaseTrace CLI. The browser download is a separate executable, not a system Python dependency.

Verify the interpreter and run development checks:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
```

## Discovery configuration

Gemini is the primary discovery provider when `GEMINI_API_KEY` and `CASETRACE_MODEL` are set. A local Ollama fallback is enabled when `CASETRACE_OLLAMA_MODEL` is also set; the default configuration uses `qwen3.5:4b` at `http://127.0.0.1:11434` with a 16384-token context. After a Gemini connection, timeout, rate-limit, or server failure, the current discovery run stays on Ollama. Invalid model tool output remains a visible failure instead of triggering fallback. If both Gemini settings are blank, Ollama runs alone.

`CASETRACE_DISCOVERY_TIMEOUT_SECONDS` controls the bounded end-to-end discovery deadline. The example uses 3600 seconds so CPU-only Ollama runs have time to complete; Gemini-only deployments can use a shorter value.

Install and verify the local model before discovery:

```powershell
ollama pull qwen3.5:4b
ollama run qwen3.5:4b "Reply with exactly: READY"
```

`.env.example` contains every configuration name. Credentials, `.venv`, caches, temporary runs, and the private source brief are excluded from Git.

Replay loads the project `.env`, but it does not import the provider SDK or make model calls. A real UI discovery, validated replay, and manual same-session handoff are still required before the completed system can be claimed. Connectivity probes and authored tests are not discovery evidence.
