# CaseTrace

CaseTrace is being implemented as a Python system that discovers a payment-investigation workflow through a synthetic banking UI, saves a typed capability, and replays it without a model. The implementation plan is in `docs/superpowers/plans/2026-09-11-casetrace-implementation.md`.

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

Keep `PLAYWRIGHT_BROWSERS_PATH` set to this project directory when running browser tests or CaseTrace. The browser download is a separate executable, not a system Python dependency.

Verify the interpreter and run development checks:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
```

## Discovery configuration

The chosen provider is Gemini. Set `GEMINI_API_KEY` and `CASETRACE_MODEL` in a private `.env`; `.env.example` contains the configuration names. The current model was selected through a real function-call access check. Credentials, `.venv`, caches, temporary runs, and the private source brief are excluded from Git.

Replay will not load `.env` or import the provider SDK. A real UI discovery, validated replay, and manual same-session handoff are still required before the completed system can be claimed. Connectivity probes and authored tests are not discovery evidence.
