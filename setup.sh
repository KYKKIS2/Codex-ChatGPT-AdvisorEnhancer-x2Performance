#!/usr/bin/env bash
set -euo pipefail
umask 077

GPT4FREE_URL="${GPT4FREE_URL:-https://github.com/xtekky/gpt4free.git}"
GPT4FREE_REF="${GPT4FREE_REF:-883c717437c4d91b68869359ed05b0427f34df65}"
DEVSPACE_VERSION="${DEVSPACE_VERSION:-1.0.4}"
ADVISOR_ALLOW_UNVERIFIED_VENDOR="${ADVISOR_ALLOW_UNVERIFIED_VENDOR:-false}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$ROOT/vendor"
G4F="$VENDOR/gpt4free"
VENV="$G4F/.venv"
PY="$VENV/bin/python"
PATCH="$ROOT/patches/gpt4free-advisor.patch"
RUNTIME_PATCH="$ROOT/patches/apply_gpt4free_runtime_patch.py"
DEVSPACE_PATCH="$ROOT/codex-skill/external-advisor/scripts/devspace_readonly_patch.py"
SKILLS_SOURCE="$ROOT/codex-skill"
SKILLS_DEST="${CODEX_HOME:-$HOME/.codex}/skills"
EXTERNAL_ADVISOR_SKILL_DEST="$SKILLS_DEST/external-advisor"
SKILL_CONFIG="$EXTERNAL_ADVISOR_SKILL_DEST/advisor-config.json"

REQUIRED_EXECUTABLES=(
  "$ROOT/start-g4f.sh"
  "$ROOT/tests/test-advisor.sh"
  "$ROOT/tests/test-conclave.sh"
  "$ROOT/tests/test-router.sh"
  "$ROOT/tests/test-context-pack.sh"
  "$ROOT/tests/test-verifier-loop.sh"
  "$ROOT/tests/test-memory.sh"
  "$ROOT/tests/test-ranking.sh"
  "$ROOT/tests/test-eval-harness.sh"
  "$ROOT/tests/test-advisor-transport-recovery.sh"
  "$ROOT/tests/test-advisor-live-activity.sh"
  "$ROOT/tests/test-advisor-concurrency.sh"
  "$ROOT/tests/test-security-regressions.sh"
  "$ROOT/tests/test-agent-mode.sh"
  "$ROOT/tests/test-agent-conclave.sh"
)
REQUIRED_CORE_FILES=(
  "$PATCH"
  "$RUNTIME_PATCH"
  "$ROOT/codex-skill/external-advisor/SKILL.md"
  "$ROOT/codex-skill/external-advisor/scripts/activity_monitor.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor_background.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor_concurrency.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor_safety.py"
  "$ROOT/codex-skill/external-advisor/scripts/agent_mode.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor_agent.py"
  "$ROOT/codex-skill/external-advisor/scripts/agent_conclave.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor_agent_setup.py"
  "$ROOT/codex-skill/external-advisor/scripts/advisor_agent_connect.py"
  "$ROOT/codex-skill/external-advisor/scripts/devspace_readonly_patch.py"
  "$ROOT/codex-skill/external-advisor/scripts/conclave.py"
  "$ROOT/codex-skill/external-advisor/scripts/context_pack.py"
  "$ROOT/codex-skill/external-advisor/scripts/critique_final.py"
  "$ROOT/codex-skill/external-advisor/scripts/eval_harness.py"
  "$ROOT/codex-skill/external-advisor/scripts/g4f_pool.py"
  "$ROOT/codex-skill/external-advisor/scripts/memory_manager.py"
  "$ROOT/codex-skill/external-advisor/scripts/project_bind.py"
  "$ROOT/codex-skill/external-advisor/scripts/project_migrate.py"
  "$ROOT/codex-skill/external-advisor/scripts/router.py"
  "$ROOT/codex-skill/external-advisor/scripts/validate_conclave.py"
  "$ROOT/codex-skill/external-advisor/scripts/verifier_loop.py"
)
for required_path in "${REQUIRED_EXECUTABLES[@]}" "${REQUIRED_CORE_FILES[@]}"; do
  if [[ ! -f "$required_path" ]]; then
    echo "Required setup file is missing: $required_path" >&2
    exit 1
  fi
done

mkdir -p "$VENDOR"

EXPECTED_VENDOR_CHANGES=(
  "g4f/Provider/needs_auth/OpenaiChat.py"
  "g4f/Provider/openai/har_file.py"
  "g4f/Provider/openai/models.py"
  "g4f/api/stubs.py"
  "g4f/providers/any_model_map.py"
)

vendor_path_is_expected() {
  local candidate="$1"
  local expected
  for expected in "${EXPECTED_VENDOR_CHANGES[@]}"; do
    [[ "$candidate" == "$expected" ]] && return 0
  done
  return 1
}

if [[ ! -d "$G4F" ]]; then
  git clone "$GPT4FREE_URL" "$G4F"
  git -C "$G4F" checkout --detach "$GPT4FREE_REF"
elif [[ -d "$G4F/.git" ]]; then
  current_ref="$(git -C "$G4F" rev-parse HEAD)"
  mapfile -t vendor_changes < <(
    git -C "$G4F" status --porcelain --untracked-files=all |
      sed -E 's/^.. //' |
      sed -E 's/^.* -> //'
  )
  unexpected_vendor_changes=()
  for changed_path in "${vendor_changes[@]}"; do
    [[ -n "$changed_path" ]] || continue
    if ! vendor_path_is_expected "$changed_path"; then
      unexpected_vendor_changes+=("$changed_path")
    fi
  done
  if [[ ${#unexpected_vendor_changes[@]} -gt 0 && "$ADVISOR_ALLOW_UNVERIFIED_VENDOR" != "true" ]]; then
    printf 'Refusing vendor/gpt4free with unexpected local changes:\n' >&2
    printf '  %s\n' "${unexpected_vendor_changes[@]}" >&2
    echo "Remove the changes or set ADVISOR_ALLOW_UNVERIFIED_VENDOR=true for a deliberate diagnostic." >&2
    exit 1
  fi
  if [[ "$current_ref" != "$GPT4FREE_REF" && ${#vendor_changes[@]} -gt 0 && "$ADVISOR_ALLOW_UNVERIFIED_VENDOR" != "true" ]]; then
    echo "Refusing dirty vendor/gpt4free at unexpected revision $current_ref; expected $GPT4FREE_REF." >&2
    exit 1
  fi
  if [[ "$current_ref" != "$GPT4FREE_REF" ]]; then
    git -C "$G4F" fetch origin
    git -C "$G4F" checkout --detach "$GPT4FREE_REF"
  fi
  echo "Verified vendor/gpt4free base revision: $GPT4FREE_REF"
else
  if [[ "$ADVISOR_ALLOW_UNVERIFIED_VENDOR" != "true" ]]; then
    echo "Refusing existing vendor/gpt4free without Git metadata." >&2
    echo "Recreate it from the pinned revision or set ADVISOR_ALLOW_UNVERIFIED_VENDOR=true for a deliberate diagnostic." >&2
    exit 1
  fi
  echo "Warning: using unverified vendor/gpt4free because ADVISOR_ALLOW_UNVERIFIED_VENDOR=true." >&2
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
"$PY" -m py_compile \
  g4f/api/stubs.py \
  g4f/Provider/openai/har_file.py \
  g4f/Provider/openai/models.py \
  g4f/Provider/needs_auth/OpenaiChat.py \
  g4f/providers/any_model_map.py

mkdir -p -m 700 "$G4F/har_and_cookies"
chmod 700 "$G4F/har_and_cookies"

chmod +x "${REQUIRED_EXECUTABLES[@]}"

installed_devspace_version=""
if command -v devspace >/dev/null 2>&1; then
  installed_devspace_version="$(devspace --version 2>/dev/null | tail -n 1 | tr -d '\r' || true)"
fi
if [[ "$installed_devspace_version" != "$DEVSPACE_VERSION" ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required to install DevSpace $DEVSPACE_VERSION for repo-aware advisor mode." >&2
    exit 1
  fi
  npm install --global "@waishnav/devspace@$DEVSPACE_VERSION"
fi
DEVSPACE_BIN="$(command -v devspace)"
installed_devspace_version="$(devspace --version 2>/dev/null | tail -n 1 | tr -d '\r' || true)"
if [[ "$installed_devspace_version" != "$DEVSPACE_VERSION" ]]; then
  echo "DevSpace version verification failed: expected $DEVSPACE_VERSION, got ${installed_devspace_version:-unknown}." >&2
  exit 1
fi
python3 "$DEVSPACE_PATCH" --executable "$DEVSPACE_BIN"

mkdir -p "$SKILLS_DEST"
BACKUP_ROOT="${CODEX_HOME:-$HOME/.codex}/skill-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p -m 700 "$BACKUP_ROOT"
exec 9>"$SKILLS_DEST/.advisor-skill-install.lock"
if command -v flock >/dev/null 2>&1; then
  flock 9
fi
for skill_dir in "$SKILLS_SOURCE"/*; do
  [[ -d "$skill_dir" ]] || continue
  skill_name="$(basename "$skill_dir")"
  dest="$SKILLS_DEST/$skill_name"
  stage="$(mktemp -d "$SKILLS_DEST/.${skill_name}.staging.XXXXXX")"
  cp -R "$skill_dir"/. "$stage"/
  if [[ "$skill_name" == "external-advisor" ]]; then
    for required_name in \
      SKILL.md \
      scripts/advisor.py \
      scripts/advisor_concurrency.py \
      scripts/advisor_safety.py \
      scripts/router.py \
      scripts/advisor_agent.py \
      scripts/agent_conclave.py \
      scripts/devspace_readonly_patch.py; do
      if [[ ! -f "$stage/$required_name" ]]; then
        rm -rf "$stage"
        echo "Staged external-advisor skill is incomplete: $required_name" >&2
        exit 1
      fi
    done
  fi
  backup=""
  if [[ -e "$dest" ]]; then
    backup="$BACKUP_ROOT/$skill_name"
    mv "$dest" "$backup"
  fi
  if ! mv "$stage" "$dest"; then
    [[ -n "$backup" && -e "$backup" ]] && mv "$backup" "$dest"
    exit 1
  fi
  echo "Installed Codex skill: $skill_name"
  [[ -z "$backup" ]] || echo "Previous skill preserved at: $backup"
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
    "worker_mode": "transient",
    "control_workers": 1,
    "max_transient_workers": 32,
    "remote_max_concurrency": 2,
    "remote_start_interval_seconds": 2,
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
