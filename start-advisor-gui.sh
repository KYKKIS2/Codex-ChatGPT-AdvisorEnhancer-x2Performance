#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
G4F="$ROOT/vendor/gpt4free"
PY="$G4F/.venv/bin/python"
GUI="$ROOT/codex-skill/external-advisor/scripts/advisor_gui.py"
RUNTIME_PATCH="$ROOT/patches/apply_gpt4free_runtime_patch.py"

if [[ ! -d "$G4F/g4f" || ! -x "$PY" ]]; then
  "$ROOT/setup.sh"
fi

python3 "$RUNTIME_PATCH" "$G4F" >/dev/null
exec "$PY" "$GUI" serve "$@"
