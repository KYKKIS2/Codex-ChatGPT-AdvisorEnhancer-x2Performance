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

if [[ ! -d "$G4F/.git" ]]; then
  git clone "$GPT4FREE_URL" "$G4F"
fi

cd "$G4F"

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv

if grep -q 'temporary: Optional\[bool\]' g4f/api/stubs.py && grep -q 'using generated proof token fallback' g4f/Provider/openai/har_file.py; then
  echo "gpt4free advisor patch already applied."
else
  git apply "$PATCH"
fi

mkdir -p "$G4F/har_and_cookies"

mkdir -p "$SKILL_DEST"
cp -R "$SKILL_SOURCE"/. "$SKILL_DEST"/
cat > "$SKILL_CONFIG" <<EOF
{
  "setup_dir": "$ROOT",
  "start_g4f": "$ROOT/start-g4f.sh",
  "base_url": "http://localhost:8080/v1",
  "model": "gpt-5-thinking"
}
EOF

cat <<EOF

Setup complete.
Next steps:
1. Put your ChatGPT HAR file in: $G4F/har_and_cookies
2. Start the local API: ./start-g4f.sh
3. Restart Codex so it discovers the external-advisor skill.
EOF
