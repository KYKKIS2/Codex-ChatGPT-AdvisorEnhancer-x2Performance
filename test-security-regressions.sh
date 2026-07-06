#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT

git -C "$PROJECT" init >/dev/null
git -C "$PROJECT" config user.email "advisor-test@example.invalid"
git -C "$PROJECT" config user.name "Advisor Test"
printf 'TOKEN=old\n' > "$PROJECT/.env"
git -C "$PROJECT" add .env
git -C "$PROJECT" commit -m "track env for regression test" >/dev/null
printf 'TOKEN=supersecret-value-that-must-not-leak\n' > "$PROJECT/.env"
printf 'SAFE=old\n' > "$PROJECT/safe.txt"
git -C "$PROJECT" add safe.txt
git -C "$PROJECT" commit -m "track safe file" >/dev/null
printf 'SAFE=new\n' > "$PROJECT/safe.txt"
printf 'STAGED_TOKEN=old\n' > "$PROJECT/.env.staged"
git -C "$PROJECT" add .env.staged
printf 'RENAMED_TOKEN=renamed-secret-that-must-not-leak\n' > "$PROJECT/.env.rename"
git -C "$PROJECT" add .env.rename
printf 'DELETED_TOKEN=deleted-secret-that-must-not-leak\n' > "$PROJECT/.env.delete"
git -C "$PROJECT" add .env.delete
git -C "$PROJECT" commit -m "track renamed env" >/dev/null
printf 'STAGED_TOKEN=staged-secret-that-must-not-leak\n' > "$PROJECT/.env.staged"
git -C "$PROJECT" add .env.staged
git -C "$PROJECT" mv .env.rename .env.renamed
git -C "$PROJECT" rm .env.delete >/dev/null
printf 'RENAMED_TOKEN=renamed-secret-that-must-not-leak-2\n' > "$PROJECT/.env.renamed"

CONTEXT_JSON="$(python3 "$SCRIPTS/context_pack.py" \
  --project-dir "$PROJECT" \
  --prompt "Check diff redaction." \
  --json)"
if grep -R "supersecret-value-that-must-not-leak\\|staged-secret-that-must-not-leak\\|renamed-secret-that-must-not-leak\\|deleted-secret-that-must-not-leak" "$PROJECT/.codex-advisor"; then
  echo "Sensitive .env diff content leaked into context pack artifacts." >&2
  exit 1
fi
python3 - "$CONTEXT_JSON" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
skipped = data.get("git", {}).get("skipped_sensitive_diff_files") or []
expected = {".env", ".env.staged", ".env.renamed", ".env.delete"}
if not expected.issubset(set(skipped)):
    raise SystemExit(f"Expected .env to be skipped from full diff, got {skipped!r}")
PY

OUTSIDE="$(mktemp -d)"
printf 'outside secret\n' > "$OUTSIDE/outside.txt"
ln -s "$OUTSIDE/outside.txt" "$PROJECT/link-outside"
set +e
python3 "$SCRIPTS/critique_final.py" \
  --project-dir "$PROJECT" \
  --dry-run \
  --draft "Draft." \
  --context-file link-outside >/tmp/advisor-symlink-out.txt 2>/tmp/advisor-symlink-err.txt
status=$?
set -e
if [[ "$status" -eq 0 ]]; then
  echo "Symlink escape context file was accepted." >&2
  exit 1
fi

set +e
python3 "$SCRIPTS/critique_final.py" \
  --project-dir "$PROJECT" \
  --dry-run \
  --draft "Draft." \
  --context-file .env >/tmp/advisor-critique-out.txt 2>/tmp/advisor-critique-err.txt
status=$?
set -e
if [[ "$status" -eq 0 ]]; then
  echo "Sensitive context file was accepted by critique_final.py." >&2
  exit 1
fi
if ! grep -q "Refusing to include" /tmp/advisor-critique-err.txt; then
  echo "Sensitive context-file refusal did not include a useful error." >&2
  exit 1
fi

PWNED="$PROJECT/pwned"
PWNED2="$PROJECT/pwned2"
python3 "$SCRIPTS/verifier_loop.py" \
  --project-dir "$PROJECT" \
  --dry-run \
  --no-sync \
  --prompt "Verify command safety." \
  --command "python3 -c \"open('$PWNED','w').write('x')\"" \
  --command "pytest \$(touch '$PWNED2')" \
  --command "npm run arbitrary-script" \
  --command "python3 --version" >/tmp/advisor-verifier-out.txt
if [[ -e "$PWNED" || -e "$PWNED2" ]]; then
  echo "Unsafe verifier command executed." >&2
  exit 1
fi
python3 - "$PROJECT/.codex-advisor/latest-verifier-loop.json" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
statuses = [item.get("status") for item in data.get("command_results", [])]
if statuses[:3] != ["skipped", "skipped", "skipped"] or statuses[-1:] != ["completed"]:
    raise SystemExit(f"Unexpected verifier command statuses: {statuses!r}")
PY

PYTHONPATH="$SCRIPTS" ADVISOR_PROJECT_DIR="$PROJECT" ADVISOR_AUTO_CREATE_PROJECT=false python3 - "$PROJECT" <<'PY'
import os
import sys
from pathlib import Path
import advisor

project = Path(sys.argv[1]).resolve()
os.environ["ADVISOR_CONVERSATION_KEY"] = "../evil/key"
state = advisor.default_state_path().resolve()
expected = (project / ".codex-advisor" / "conversations").resolve()
if expected not in (state.parent, *state.parents):
    raise SystemExit(f"Conversation key escaped project state: {state}")
if ".." in state.name or "/" in state.name:
    raise SystemExit(f"Conversation key was not sanitized: {state.name}")
os.environ["ADVISOR_CONVERSATION_KEY"] = "..evil/key"
other = advisor.default_state_path().resolve()
if other == state:
    raise SystemExit("Distinct unsafe conversation keys collided after sanitization.")
PY

PYTHONPATH="$SCRIPTS" OPENAI_API_KEY="sk-test-openai-key-that-must-not-be-forwarded" python3 - <<'PY'
import os
import advisor

os.environ.pop("ADVISOR_API_KEY", None)
os.environ.pop("ADVISOR_COMPATIBLE_USE_OPENAI_KEY", None)
if advisor.compatible_api_key("http://127.0.0.1:8080/v1") != "local":
    raise SystemExit("Compatible local endpoint inherited OPENAI_API_KEY.")
if advisor.compatible_api_key("https://api.openai.com/v1") != "local":
    raise SystemExit("OpenAI host inherited OPENAI_API_KEY without explicit opt-in.")
os.environ["ADVISOR_COMPATIBLE_USE_OPENAI_KEY"] = "true"
if advisor.compatible_api_key("https://api.openai.com/v1") != os.environ["OPENAI_API_KEY"]:
    raise SystemExit("OpenAI key opt-in did not work.")
os.environ["ADVISOR_API_KEY"] = "explicit-advisor-key"
if advisor.compatible_api_key("http://127.0.0.1:8080/v1") != "explicit-advisor-key":
    raise SystemExit("Explicit ADVISOR_API_KEY was not honored.")
PY

PYTHONPATH="$SCRIPTS" python3 - "$PROJECT" <<'PY'
import json
import sys
from pathlib import Path
import advisor

state = Path(sys.argv[1]) / ".codex-advisor" / "conversation.json"
state.parent.mkdir(parents=True, exist_ok=True)
transcript = advisor.transcript_json_path(state)
payload = {
    "messages": [
        {"role": "user", "content": "prompt", "status": "finished_successfully"},
        {"role": "assistant", "content": "partial", "status": "in_progress"},
    ]
}
transcript.write_text(json.dumps(payload), encoding="utf-8")
if advisor.latest_transcript_assistant_text_for_prompt(state, "prompt"):
    raise SystemExit("Unfinished transcript assistant text was accepted.")
payload["messages"].append({"role": "assistant", "content": "finished", "status": "finished_successfully"})
transcript.write_text(json.dumps(payload), encoding="utf-8")
if advisor.latest_transcript_assistant_text_for_prompt(state, "prompt") != "finished":
    raise SystemExit("Finished transcript assistant text was not recovered.")
if not advisor.should_prefer_synced_text("ADVISOR_OKADVISOR_OK", "ADVISOR_OK"):
    raise SystemExit("Duplicated compatible transport body was not recognized as recoverable from transcript.")
if advisor.deduplicate_repeated_transport_text("ADVISOR_OKADVISOR_OK") != "ADVISOR_OK":
    raise SystemExit("Duplicated compatible transport body was not deduplicated.")
keyed = state.parent / "audit-key.conversation.json"
if advisor.transcript_json_path(keyed).name != "audit-key.transcript.json":
    raise SystemExit("Keyed advisor state did not get a keyed transcript JSON path.")
if advisor.transcript_md_path(keyed).name != "audit-key.transcript.md":
    raise SystemExit("Keyed advisor state did not get a keyed transcript Markdown path.")
long_prompt = "x" * 500
if not advisor.response_needs_remote_recovery("Yes orders Do", long_prompt, had_transport_corruption=True):
    raise SystemExit("Suspicious duplicated transport fragment did not require remote recovery.")
if advisor.response_needs_remote_recovery("Short but legitimate.", long_prompt, had_transport_corruption=False):
    raise SystemExit("Legitimate short response was incorrectly marked as transport corruption.")
conversation_data = {
    "mapping": {
        "u1": {"id": "u1", "parent": None, "message": {"id": "u1", "author": {"role": "user"}, "content": {"parts": ["old prompt"]}, "status": "finished_successfully"}},
        "a1": {"id": "a1", "parent": "u1", "message": {"id": "a1", "author": {"role": "assistant"}, "content": {"parts": ["old answer"]}, "status": "finished_successfully"}},
    },
    "current_node": "a1",
}
if advisor.latest_finished_assistant_text_for_prompt_data(conversation_data, "new prompt"):
    raise SystemExit("Remote final fetch would have accepted stale assistant text for the wrong prompt.")
conversation_data["mapping"]["u2"] = {"id": "u2", "parent": "a1", "message": {"id": "u2", "author": {"role": "user"}, "content": {"parts": ["new prompt"]}, "status": "finished_successfully"}}
conversation_data["mapping"]["a2"] = {"id": "a2", "parent": "u2", "message": {"id": "a2", "author": {"role": "assistant"}, "content": {"parts": ["new answer"]}, "status": "finished_successfully"}}
conversation_data["current_node"] = "a2"
if advisor.latest_finished_assistant_text_for_prompt_data(conversation_data, "new prompt") != "new answer":
    raise SystemExit("Remote final fetch did not recover assistant text for the matching prompt.")
if advisor.latest_finished_assistant_text_for_prompt_data(conversation_data, "old prompt") != "old answer":
    raise SystemExit("Prompt-matched recovery crossed into a later unrelated user turn.")
nested = {"choices": [{"message": {"metadata": {"conversation": {"conversation_id": "abc", "message_id": "m"}}}}]}
if advisor.find_conversation_payload(nested).get("conversation_id") != "abc":
    raise SystemExit("Nested conversation payload was not discovered.")
if not advisor.find_conversation_data_payload(conversation_data):
    raise SystemExit("Full conversation-data payload was not discovered.")
PY

echo "Security regression tests passed."
