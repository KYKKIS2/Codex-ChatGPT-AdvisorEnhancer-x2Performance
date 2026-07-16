#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONCLAVE="$ROOT/codex-skill/external-advisor/scripts/conclave.py"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT
RUNS="$PROJECT/.codex-advisor/conclave-runs"

python3 "$CONCLAVE" \
  --project-dir "$PROJECT" \
  --dry-run \
  --machine-json \
  --no-synthesis \
  --mode "model-choice" \
  --roles "planner,critic" \
  --prompt "Smoke test ranking of advisor outputs."

python3 - "$RUNS" <<'PY'
import json
import sys
from pathlib import Path

runs = Path(sys.argv[1])
candidates = sorted(runs.glob("*.json"), key=lambda path: path.stat().st_mtime)
if not candidates:
    raise SystemExit("Expected a conclave run JSON file.")
data = json.loads(candidates[-1].read_text(encoding="utf-8"))
rankings = (data.get("ranking") or {}).get("role_rankings") or []
if len(rankings) < 2:
    raise SystemExit("Expected ranking.role_rankings for planner and critic.")
if "confidence" not in (data.get("ranking") or {}).get("criteria", []):
    raise SystemExit("Expected confidence ranking criterion.")
print("Ranking smoke test passed.")
PY
