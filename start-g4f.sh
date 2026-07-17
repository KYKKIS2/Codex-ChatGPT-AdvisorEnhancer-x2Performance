#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-${G4F_MODEL:-gpt-5-6-thinking}}"
PROVIDER="${G4F_PROVIDER:-OpenaiAccount}"
PORT="${G4F_PORT:-8080}"
MODE="${G4F_WORKER_MODE:-transient}"
WORKERS="${G4F_WORKERS:-1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
G4F="$ROOT/vendor/gpt4free"
PY="$G4F/.venv/bin/python"
POOL="$ROOT/codex-skill/external-advisor/scripts/g4f_pool.py"

if [[ ! -d "$G4F/g4f" ]]; then
  SETUP="$ROOT/setup.sh"
  if [[ ! -f "$SETUP" ]]; then
    echo "gpt4free is not installed and setup.sh was not found." >&2
    exit 1
  fi
  echo "gpt4free is not installed. Running setup.sh first..."
  "$SETUP"
  if [[ ! -d "$G4F/g4f" ]]; then
    echo "setup.sh completed but gpt4free is still missing at $G4F" >&2
    exit 1
  fi
fi

export G4F_PROVIDER="$PROVIDER"
export G4F_MODEL="$MODEL"

if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

RUNTIME_PATCH="$ROOT/patches/apply_gpt4free_runtime_patch.py"
if [[ -f "$RUNTIME_PATCH" ]]; then
  python3 "$RUNTIME_PATCH" "$G4F" >/dev/null
fi

if [[ ! -f "$POOL" ]]; then
  echo "g4f worker-pool supervisor was not found: $POOL" >&2
  exit 1
fi

ARGS=(
  "$POOL" serve
  --python "$PY"
  --g4f-dir "$G4F"
  --port "$PORT"
  --mode "$MODE"
  --workers "$WORKERS"
  --model "$MODEL"
  --provider "$PROVIDER"
)
if [[ "${G4F_DEBUG:-}" =~ ^(1|true|yes|on)$ ]]; then
  ARGS+=(--debug)
fi
exec "$PY" "${ARGS[@]}"
