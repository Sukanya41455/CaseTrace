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

Run the demo:

```powershell
.\scripts\demo.ps1
```

Useful demo options:

```powershell
.\scripts\demo.ps1 -Headless
.\scripts\demo.ps1 -UseExistingFixture
```

## What this does

- Opens the synthetic bank in a browser
- Explores the visible UI
- Records a typed, bounded capability
- Replays the workflow without model calls

## Development checks

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
```

## Discovery config

If you want live discovery, set env values from `.env.example`.

Common options:
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
- The reliable demo path is the authored, validated replay flow.
- Live discovery is optional and slower than replay.
