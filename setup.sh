#!/usr/bin/env bash
set -euo pipefail

GPT4FREE_URL="${GPT4FREE_URL:-https://github.com/xtekky/gpt4free.git}"
GPT4FREE_REF="${GPT4FREE_REF:-883c717437c4d91b68869359ed05b0427f34df65}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$ROOT/vendor"
G4F="$VENDOR/gpt4free"
VENV="$G4F/.venv"
PY="$VENV/bin/python"
PATCH="$ROOT/patches/gpt4free-advisor.patch"
RUNTIME_PATCH="$ROOT/patches/apply_gpt4free_runtime_patch.py"
SKILLS_SOURCE="$ROOT/codex-skill"
SKILLS_DEST="${CODEX_HOME:-$HOME/.codex}/skills"
EXTERNAL_ADVISOR_SKILL_DEST="$SKILLS_DEST/external-advisor"
SKILL_CONFIG="$EXTERNAL_ADVISOR_SKILL_DEST/advisor-config.json"

mkdir -p "$VENDOR"

if [[ ! -d "$G4F" ]]; then
  git clone "$GPT4FREE_URL" "$G4F"
  git -C "$G4F" checkout --detach "$GPT4FREE_REF"
elif [[ -d "$G4F/.git" ]]; then
  if git -C "$G4F" diff --quiet && git -C "$G4F" diff --cached --quiet; then
    git -C "$G4F" fetch origin
    git -C "$G4F" checkout --detach "$GPT4FREE_REF"
  else
    echo "Using existing patched vendor/gpt4free checkout without resetting local edits."
  fi
else
  echo "Using existing vendor/gpt4free directory without Git metadata."
fi

cd "$G4F"

if [[ ! -x "$PY" ]]; then
  python3 -m venv "$VENV"
fi
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt
"$PY" -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv

if grep -q 'temporary: Optional\[bool\]' g4f/api/stubs.py && grep -q 'using generated proof token fallback' g4f/Provider/openai/har_file.py; then
  echo "gpt4free base advisor patch already applied."
else
  git apply --check --recount "$PATCH"
  git apply --recount "$PATCH"
fi

python3 "$RUNTIME_PATCH" "$G4F"

mkdir -p "$G4F/har_and_cookies"

chmod +x \
  "$ROOT/start-g4f.sh" \
  "$ROOT/test-advisor.sh" \
  "$ROOT/test-conclave.sh" \
  "$ROOT/test-router.sh" \
  "$ROOT/test-context-pack.sh" \
  "$ROOT/test-verifier-loop.sh" \
  "$ROOT/test-memory.sh" \
  "$ROOT/test-ranking.sh" \
  "$ROOT/test-eval-harness.sh" \
  "$ROOT/test-advisor-transport-recovery.sh" \
  "$ROOT/test-advisor-live-activity.sh" \
  "$ROOT/test-security-regressions.sh" \
  "$ROOT/test-agent-mode.sh" \
  "$ROOT/codex-skill/external-advisor/scripts/agent_mode.py" \
  "$ROOT/codex-skill/external-advisor/scripts/advisor_agent_setup.py" \
  "$ROOT/codex-skill/external-advisor/scripts/advisor_agent_connect.py" 2>/dev/null || true

mkdir -p "$SKILLS_DEST"
for skill_dir in "$SKILLS_SOURCE"/*; do
  [[ -d "$skill_dir" ]] || continue
  skill_name="$(basename "$skill_dir")"
  dest="$SKILLS_DEST/$skill_name"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -R "$skill_dir"/. "$dest"/
  echo "Installed Codex skill: $skill_name"
done
ROOT="$ROOT" START_G4F="$ROOT/start-g4f.sh" SKILL_CONFIG="$SKILL_CONFIG" python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "setup_dir": os.environ["ROOT"],
    "start_g4f": os.environ["START_G4F"],
    "base_url": "http://127.0.0.1:8080/v1",
    "model": "gpt-5-6-thinking",
}
path = Path(os.environ["SKILL_CONFIG"])
path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY

cat <<EOF

Setup complete.
Pinned gpt4free ref: $GPT4FREE_REF
Next steps:
1. Put your ChatGPT HAR file in: $G4F/har_and_cookies
2. Start the local API: ./start-g4f.sh
3. For repo-aware ChatGPT agent mode, run from a project:
   python3 ~/.codex/skills/external-advisor/scripts/advisor_agent_connect.py serve --project-dir .
   Then paste the printed /mcp URL into ChatGPT Developer Mode.
4. Restart Codex so it discovers the bundled skills.
EOF
