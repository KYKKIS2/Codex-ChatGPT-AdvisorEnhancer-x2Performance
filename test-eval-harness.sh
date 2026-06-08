#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_HARNESS="$ROOT/codex-skill/external-advisor/scripts/eval_harness.py"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT
LATEST="$PROJECT/.codex-advisor/latest-evaluation.json"

python3 "$EVAL_HARNESS" --project-dir "$PROJECT" --dry-run --limit-per-category 1 --strategy all

python3 - "$LATEST" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"Expected evaluation output was not written: {path}")
data = json.loads(path.read_text(encoding="utf-8"))
if len(data.get("results", [])) != 16:
    raise SystemExit(f"Expected 16 results for 4 categories x 4 strategies, got {len(data.get('results', []))}.")
if len(data.get("summary", [])) != 4:
    raise SystemExit("Expected 4 strategy summaries.")
categories = set(data.get("categories", []))
if not {"architecture", "model-choice"}.issubset(categories):
    raise SystemExit("Expected benchmark categories.")
print("Evaluation harness smoke test passed.")
PY
