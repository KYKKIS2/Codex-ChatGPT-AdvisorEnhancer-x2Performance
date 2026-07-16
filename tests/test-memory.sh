#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMORY="$ROOT/codex-skill/external-advisor/scripts/memory_manager.py"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT
ADVISOR_DIR="$PROJECT/.codex-advisor"

python3 "$MEMORY" --project-dir "$PROJECT" init
python3 "$MEMORY" --project-dir "$PROJECT" record-decision \
  --id "memory-smoke-decision" \
  --decision "Use evidence-backed verifier loops for failed tests." \
  --rationale "Verifier advice should be connected to command output." \
  --source "test-memory.sh" \
  --confidence 0.9 \
  --status "accepted" \
  --tag "verifier"
python3 "$MEMORY" --project-dir "$PROJECT" record-outcome \
  --id "memory-smoke-outcome" \
  --task "Smoke test memory manager." \
  --advisor-mode "verifier-loop" \
  --accepted-advice "Run a harmless command." \
  --rejected-advice "Do not run unsafe shell commands." \
  --outcome "Memory files were written." \
  --useful "true" \
  --source "test-memory.sh" \
  --confidence 0.8 \
  --status "accepted"
python3 "$MEMORY" --project-dir "$PROJECT" summary

python3 - "$ADVISOR_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
decisions = json.loads((root / "decisions.json").read_text(encoding="utf-8"))
outcomes = json.loads((root / "outcomes.json").read_text(encoding="utf-8"))
if not any(item.get("id") == "memory-smoke-decision" for item in decisions):
    raise SystemExit("Expected smoke decision in decisions.json.")
if not any(item.get("id") == "memory-smoke-outcome" for item in outcomes):
    raise SystemExit("Expected smoke outcome in outcomes.json.")
if not (root / "memory-summary.md").exists():
    raise SystemExit("Expected memory-summary.md.")
print("Memory manager smoke test passed.")
PY
