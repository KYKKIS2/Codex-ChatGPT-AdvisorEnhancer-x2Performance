#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-gpt-5-6-thinking}"
PORT="${G4F_PORT:-8080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADVISOR="$ROOT/codex-skill/external-advisor/scripts/advisor.py"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT

export ADVISOR_PROVIDER="openai-compatible"
export ADVISOR_BASE_URL="http://127.0.0.1:$PORT/v1"
export ADVISOR_MODEL="$MODEL"
export ADVISOR_REASONING_EFFORT="high"
export ADVISOR_MAX_OUTPUT_TOKENS="500"
export ADVISOR_PROJECT_DIR="$PROJECT"
export ADVISOR_AUTO_CREATE_PROJECT="false"
export ADVISOR_PERSIST_CONVERSATION="false"
export ADVISOR_TEMPORARY="true"
export ADVISOR_SYNC_REMOTE="false"
export ADVISOR_AUTO_RETRY_TAIL_FRAGMENT="false"

python3 "$ADVISOR" --prompt "Smoke test. Reply with ADVISOR_SETUP_OK and one short sentence."
