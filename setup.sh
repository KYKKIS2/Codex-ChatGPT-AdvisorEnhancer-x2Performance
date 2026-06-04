#!/usr/bin/env bash
set -euo pipefail

GPT4FREE_URL="${GPT4FREE_URL:-https://github.com/xtekky/gpt4free.git}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$ROOT/vendor"
G4F="$VENDOR/gpt4free"
PATCH="$ROOT/patches/gpt4free-advisor.patch"
SKILL_SOURCE="$ROOT/codex-skill/external-advisor"
SKILL_DEST="${CODEX_HOME:-$HOME/.codex}/skills/external-advisor"
SKILL_CONFIG="$SKILL_DEST/advisor-config.json"

mkdir -p "$VENDOR"

if [[ ! -d "$G4F" ]]; then
  git clone "$GPT4FREE_URL" "$G4F"
elif [[ ! -d "$G4F/.git" ]]; then
  echo "Using existing vendor/gpt4free directory without Git metadata."
fi

cd "$G4F"

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv

if grep -q 'temporary: Optional\[bool\]' g4f/api/stubs.py && grep -q 'using generated proof token fallback' g4f/Provider/openai/har_file.py; then
  echo "gpt4free base advisor patch already applied."
else
  git apply "$PATCH"
fi

if ! grep -q 'gizmo_id: Optional\[str\]' g4f/api/stubs.py; then
  python3 - <<'PY'
from pathlib import Path

path = Path("g4f/api/stubs.py")
text = path.read_text(encoding="utf-8")
needle = "    extra_body: Optional[dict] = None\n"
replacement = (
    "    extra_body: Optional[dict] = None\n"
    "    gizmo_id: Optional[str] = None\n"
    "    conversation_mode: Optional[dict] = None\n"
)
if needle not in text:
    raise SystemExit("Could not find extra_body field in g4f/api/stubs.py")
path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
PY
  echo "Added gpt4free ChatGPT Project passthrough fields."
fi

mkdir -p "$G4F/har_and_cookies"

chmod +x "$ROOT/start-g4f.sh" "$ROOT/test-advisor.sh" "$ROOT/test-conclave.sh" "$ROOT/test-router.sh" "$ROOT/test-context-pack.sh" "$ROOT/test-verifier-loop.sh" "$ROOT/test-memory.sh" "$ROOT/test-ranking.sh" "$ROOT/test-eval-harness.sh" 2>/dev/null || true

mkdir -p "$SKILL_DEST"
cp -R "$SKILL_SOURCE"/. "$SKILL_DEST"/
cat > "$SKILL_CONFIG" <<EOF
{
  "setup_dir": "$ROOT",
  "start_g4f": "$ROOT/start-g4f.sh",
  "base_url": "http://127.0.0.1:8080/v1",
  "model": "gpt-5-5-thinking"
}
EOF

cat <<EOF

Setup complete.
Next steps:
1. Put your ChatGPT HAR file in: $G4F/har_and_cookies
2. Start the local API: ./start-g4f.sh
3. Restart Codex so it discovers the external-advisor skill.
EOF
