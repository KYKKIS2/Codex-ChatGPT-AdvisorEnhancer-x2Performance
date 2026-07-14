#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFIER_LOOP="$ROOT/codex-skill/external-advisor/scripts/verifier_loop.py"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT
LATEST="$PROJECT/.codex-advisor/latest-verifier-loop.json"

export ADVISOR_PROVIDER="openai-compatible"
export ADVISOR_BASE_URL="${ADVISOR_BASE_URL:-http://127.0.0.1:8080/v1}"
export ADVISOR_MODEL="${ADVISOR_MODEL:-gpt-5-6-thinking}"
export ADVISOR_REASONING_EFFORT="high"
export ADVISOR_MAX_OUTPUT_TOKENS="700"

python3 "$VERIFIER_LOOP" \
  --project-dir "$PROJECT" \
  --dry-run \
  --no-sync \
  --prompt "Smoke test the evidence-backed verifier loop." \
  --draft "Plan: run a harmless local command and ask the verifier to interpret the result." \
  --command "python3 --version"

python3 - "$LATEST" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"Expected verifier loop output was not written: {path}")
data = json.loads(path.read_text(encoding="utf-8"))
results = data.get("command_results") or []
if not results:
    raise SystemExit("Expected at least one command result in verifier loop output.")
if results[0].get("status") != "completed":
    raise SystemExit(f"Expected command to complete, got: {results[0].get('status')}")
if not (data.get("interpretation") or {}).get("recommendation"):
    raise SystemExit("Expected verifier interpretation recommendation.")
print("Verifier loop smoke test passed.")
PY
