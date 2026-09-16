#!/usr/bin/env bash

set -euo pipefail

headless=false
use_existing_fixture=false

usage() {
  echo "Usage: bash scripts/demo.sh [--headless] [--use-existing-fixture]" >&2
}

while (($#)); do
  case "$1" in
    --headless)
      headless=true
      ;;
    --use-existing-fixture)
      use_existing_fixture=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$project_root/.venv/bin/python"
artifact="$project_root/evidence/validation/capability.json"
query="$project_root/examples/queries/posted-new-member.json"
bindings="$project_root/config/base.json"
target="http://127.0.0.1:8000"
stamp="$(date +%Y%m%d-%H%M%S)"
output="$project_root/runs/demo-$stamp"
fixture_pid=""

if [[ ! -x "$python" ]]; then
  echo "Project environment not found. Run the README setup commands first." >&2
  exit 1
fi
if [[ ! -f "$artifact" ]]; then
  echo "Validated demo artifact not found." >&2
  exit 1
fi

is_fixture_ready() {
  "$python" - "$target" <<'PY'
import sys
from urllib.request import urlopen

try:
    with urlopen(sys.argv[1], timeout=1) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

cleanup() {
  if [[ -n "$fixture_pid" ]] && kill -0 "$fixture_pid" 2>/dev/null; then
    kill "$fixture_pid"
    wait "$fixture_pid" || true
  fi
}
trap cleanup EXIT

cd "$project_root"

if [[ "$use_existing_fixture" == false ]]; then
  if is_fixture_ready; then
    echo "Port 8000 is already serving HTTP. Use --use-existing-fixture only for the normal CaseTrace fixture." >&2
    exit 1
  fi
  mkdir -p "$project_root/runs"
  "$python" -m casetrace.cli fixture --scenario normal --port 8000 \
    >"$project_root/runs/.demo-fixture-$stamp.out.log" \
    2>"$project_root/runs/.demo-fixture-$stamp.err.log" &
  fixture_pid=$!
fi

for ((attempt = 0; attempt < 40; attempt++)); do
  if [[ -n "$fixture_pid" ]] && ! kill -0 "$fixture_pid" 2>/dev/null; then
    echo "Synthetic bank process exited before it became ready." >&2
    exit 1
  fi
  if is_fixture_ready; then
    break
  fi
  sleep 0.25
done

if ! is_fixture_ready; then
  echo "Synthetic bank did not become ready on $target." >&2
  exit 1
fi

echo "Running CaseTrace against the synthetic bank..."
replay_args=(
  -m casetrace.cli replay "$artifact"
  --target "$target"
  --bindings "$bindings"
  --params "$query"
  --output "$output"
)
if [[ "$headless" == false ]]; then
  replay_args+=(--headed)
fi
"$python" "${replay_args[@]}"

"$python" - "$output" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
result = json.loads((output / "result.json").read_text(encoding="utf-8"))
if result.get("kind") != "success" or result.get("payment", {}).get("status") != "POSTED":
    raise SystemExit("Demo did not produce the expected POSTED success.")

for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines():
    if json.loads(line).get("model_call_count") != 0:
        raise SystemExit("Demo replay unexpectedly recorded a model call.")
PY

echo "Demo complete. Evidence: $output"
