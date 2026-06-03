#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-gpt-5-5-thinking}"
PORT="${G4F_PORT:-8080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADVISOR="$ROOT/codex-skill/external-advisor/scripts/advisor.py"

export ADVISOR_PROVIDER="openai-compatible"
export ADVISOR_BASE_URL="http://localhost:$PORT/v1"
export ADVISOR_MODEL="$MODEL"
export ADVISOR_REASONING_EFFORT="high"
export ADVISOR_MAX_OUTPUT_TOKENS="500"

python3 "$ADVISOR" --prompt "Smoke test. Reply with ADVISOR_SETUP_OK and one short sentence."
