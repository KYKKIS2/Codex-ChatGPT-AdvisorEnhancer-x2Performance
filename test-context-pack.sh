#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT_PACK="$ROOT/codex-skill/external-advisor/scripts/context_pack.py"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT
mkdir -p "$PROJECT/.git"
cp "$ROOT/README.md" "$PROJECT/README.md"
LATEST="$PROJECT/.codex-advisor/latest-context-pack.json"

python3 "$CONTEXT_PACK" \
  --project-dir "$PROJECT" \
  --prompt "Smoke test context pack generation." \
  --draft "Plan: include README and git context." \
  --file "README.md" \
  --constraint "Keep advisor context compact."

python3 - "$LATEST" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"Expected context pack output was not written: {path}")
data = json.loads(path.read_text(encoding="utf-8"))
if data.get("task") != "Smoke test context pack generation.":
    raise SystemExit("Unexpected task in context pack.")
files = data.get("relevant_files") or []
if not files or files[0].get("path") != "README.md":
    raise SystemExit("Expected README.md in relevant_files.")
if "git" not in data:
    raise SystemExit("Expected git context in context pack.")
print("Context pack smoke test passed.")
PY
