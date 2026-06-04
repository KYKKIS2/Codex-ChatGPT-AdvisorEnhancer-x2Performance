#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-gpt-5-5-thinking}"
PORT="${G4F_PORT:-8080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONCLAVE="$ROOT/codex-skill/external-advisor/scripts/conclave.py"

export ADVISOR_PROVIDER="openai-compatible"
export ADVISOR_BASE_URL="http://127.0.0.1:$PORT/v1"
export ADVISOR_MODEL="$MODEL"
export ADVISOR_REASONING_EFFORT="high"
export ADVISOR_MAX_OUTPUT_TOKENS="700"

python3 "$CONCLAVE" --mode strategy --roles planner,critic --no-synthesis --no-sync --prompt "Smoke test. Briefly assess whether a conclave layer should stay bounded and role-based."

python3 "$CONCLAVE" --mode verification --machine-json --no-synthesis --no-sync --prompt "Smoke test. Return verification checks for the conclave setup."
python3 "$ROOT/codex-skill/external-advisor/scripts/validate_conclave.py"
