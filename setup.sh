#!/usr/bin/env bash
set -euo pipefail

GPT4FREE_URL="${GPT4FREE_URL:-https://github.com/xtekky/gpt4free.git}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$ROOT/vendor"
G4F="$VENDOR/gpt4free"
VENV="$G4F/.venv"
PY="$VENV/bin/python"
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

if ! grep -q 'thinking_effort: Optional\[str\]' g4f/api/stubs.py; then
  python3 - <<'PY'
from pathlib import Path

path = Path("g4f/api/stubs.py")
text = path.read_text(encoding="utf-8")
needle = "    reasoning_effort: Optional[Literal[\"low\", \"medium\", \"high\"]] = None\n"
replacement = needle + "    thinking_effort: Optional[str] = None\n"
if needle not in text:
    raise SystemExit("Could not find reasoning_effort field in g4f/api/stubs.py")
path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
PY
  echo "Added gpt4free ChatGPT thinking_effort passthrough field."
fi

python3 - <<'PY'
from pathlib import Path

path = Path("g4f/Provider/needs_auth/OpenaiChat.py")
text = path.read_text(encoding="utf-8")
changed = False

needle = "        reasoning_effort: Optional[str] = None,\n        **kwargs\n"
replacement = (
    "        reasoning_effort: Optional[str] = None,\n"
    "        thinking_effort: Optional[str] = None,\n"
    "        **kwargs\n"
)
if "thinking_effort: Optional[str] = None" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat.create_authed reasoning_effort parameter")
    text = text.replace(needle, replacement, 1)
    changed = True

old_derivation = (
    "            if thinking_effort is None and reasoning_effort in {\"low\", \"medium\", \"high\"}:\n"
    "                thinking_effort = reasoning_effort\n"
)
if old_derivation in text:
    text = text.replace(old_derivation, "", 1)
    changed = True

needle = (
    "            system_hints = [\"picture_v2\"] if image_model else []\n"
    "            if reasoning_effort == \"high\":\n"
    "                system_hints.append(\"reason\")\n"
    "            if web_search:\n"
)
replacement = (
    "            system_hints = [\"picture_v2\"] if image_model else []\n"
    "            if reasoning_effort == \"high\" and thinking_effort is None:\n"
    "                system_hints.append(\"reason\")\n"
    "            if web_search:\n"
)
if "if reasoning_effort == \"high\" and thinking_effort is None" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat system_hints block")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                    if temporary:\n"
    "                        data[\"history_and_training_disabled\"] = True\n"
    "                    if conversation.conversation_id is not None and not temporary:\n"
)
replacement = (
    "                    if temporary:\n"
    "                        data[\"history_and_training_disabled\"] = True\n"
    "                    if thinking_effort:\n"
    "                        data[\"thinking_effort\"] = thinking_effort\n"
    "                    if conversation.conversation_id is not None and not temporary:\n"
)
if text.count("data[\"thinking_effort\"] = thinking_effort") < 1:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat prepare payload insertion point")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                if temporary:\n"
    "                    data[\"history_and_training_disabled\"] = True\n"
    "\n"
    "                if conversation.conversation_id is not None and not temporary:\n"
)
replacement = (
    "                if temporary:\n"
    "                    data[\"history_and_training_disabled\"] = True\n"
    "                if thinking_effort:\n"
    "                    data[\"thinking_effort\"] = thinking_effort\n"
    "\n"
    "                if conversation.conversation_id is not None and not temporary:\n"
)
if text.count("data[\"thinking_effort\"] = thinking_effort") < 2:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat conversation payload insertion point")
    text = text.replace(needle, replacement, 1)
    changed = True

if changed:
    path.write_text(text, encoding="utf-8")
    print("Added gpt4free ChatGPT thinking_effort request support.")
else:
    print("gpt4free ChatGPT thinking_effort request support already applied.")
PY

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
