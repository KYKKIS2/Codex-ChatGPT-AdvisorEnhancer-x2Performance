#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
PROJECT="$(mktemp -d)"
trap 'python3 - "$PROJECT" <<'"'"'PY'"'"'
import shutil
import sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
PY' EXIT

mkdir -p "$PROJECT/.codex-advisor"

python3 "$SCRIPTS/advisor_agent.py" \
  --project-dir "$PROJECT" \
  --dry-run \
  --json \
  --role verifier \
  --prompt "Inspect one file." >"$PROJECT/agent-dry-run.json"

python3 - "$PROJECT/agent-dry-run.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("status") != "dry-run":
    raise SystemExit("advisor_agent dry run did not report dry-run status")
PY

python3 "$SCRIPTS/agent_conclave.py" \
  --project-dir "$PROJECT" \
  --dry-run \
  --json \
  --mode architecture \
  --roles architect,critic \
  --prompt "Review the architecture." >"$PROJECT/conclave-dry-run.json"

python3 - "$PROJECT/conclave-dry-run.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("status") != "ok":
    raise SystemExit("agent_conclave dry run failed")
if payload.get("roles") != ["architect", "critic"]:
    raise SystemExit("agent_conclave dry run used the wrong roles")
if not Path(payload["report_path"]).is_file():
    raise SystemExit("agent_conclave dry run did not write a report")
PY

PYTHONPATH="$SCRIPTS" python3 - "$PROJECT" <<'PY'
import json
import sys
from pathlib import Path

import advisor
import advisor_agent
import agent_conclave

project = Path(sys.argv[1])
state_path = project / "conversation.json"
state_path.write_text(json.dumps({"conversation": {"conversation_id": "test-conversation"}}), encoding="utf-8")

latest_success = project / ".codex-advisor" / "latest-agent-conclave.md"
latest_attempt = project / ".codex-advisor" / "latest-agent-conclave-attempt.md"
latest_success.write_text("previous success\n", encoding="utf-8")
agent_conclave.publish_latest_reports(project, "failed attempt\n", successful=False)
if latest_attempt.read_text(encoding="utf-8") != "failed attempt\n":
    raise SystemExit("failed agent conclave did not publish the latest attempt")
if latest_success.read_text(encoding="utf-8") != "previous success\n":
    raise SystemExit("failed agent conclave overwrote the latest successful report")
agent_conclave.publish_latest_reports(project, "new success\n", successful=True)
if latest_attempt.read_text(encoding="utf-8") != "new success\n":
    raise SystemExit("successful agent conclave did not update the latest attempt")
if latest_success.read_text(encoding="utf-8") != "new success\n":
    raise SystemExit("successful agent conclave did not update the latest successful report")

mapping = {
    "call": {
        "children": ["open-result"],
        "message": {
            "recipient": "api_tool.call_tool",
            "content": {
                "text": json.dumps({
                    "path": "mcp://devspace/open_workspace",
                    "args": {"path": str(project), "mode": "checkout"},
                })
            },
        },
    },
    "open-result": {
        "parent": "call",
        "children": ["read-call"],
        "message": {
            "author": {"role": "tool", "name": "api_tool.call_tool"},
            "status": "finished_successfully",
            "content": {"text": json.dumps({
                "workspaceId": "test",
                "root": str(project),
                "mode": "checkout",
            })},
            "metadata": {"invoked_resource": {"resource_uri": "mcp://devspace/open_workspace"}},
        },
    },
    "read-call": {
        "parent": "open-result",
        "children": ["read-result"],
        "message": {
            "recipient": "api_tool.call_tool",
            "content": {
                "text": json.dumps({
                    "path": "mcp://devspace/read",
                    "args": {"workspaceId": "test", "path": "README.md"},
                })
            },
        },
    },
    "read-result": {
        "parent": "read-call",
        "children": [],
        "message": {
            "author": {"role": "tool", "name": "api_tool.call_tool"},
            "status": "finished_successfully",
            "content": {"text": json.dumps({"result": "bounded"})},
            "metadata": {"invoked_resource": {"resource_uri": "mcp://devspace/read"}},
        },
    },
}
old_auth = advisor.load_chatgpt_auth
old_get = advisor.get_json
try:
    advisor.load_chatgpt_auth = lambda: {"headers": {}}
    advisor.get_json = lambda *_args, **_kwargs: {"mapping": mapping}
    records, error = advisor_agent.remote_tool_records(state_path, 5)
finally:
    advisor.load_chatgpt_auth = old_auth
    advisor.get_json = old_get
if error:
    raise SystemExit(error)
evidence = advisor_agent.summarize_tool_evidence(
    records,
    allow_shell=False,
    expected_workspace=project,
)
if evidence.sequence != ["open_workspace", "read"] or evidence.inspection_count != 1:
    raise SystemExit(f"wrong per-conversation tool evidence: {evidence}")

result_only_mapping = {
    "call": mapping["call"],
    "open-result": {
        **mapping["open-result"],
        "children": ["read-result"],
    },
    "read-result": {
        **mapping["read-result"],
        "parent": "open-result",
    },
}
result_only_records = advisor_agent.tool_records_from_conversation_data(
    {"mapping": result_only_mapping}
)
result_only_evidence = advisor_agent.summarize_tool_evidence(
    result_only_records,
    allow_shell=False,
    expected_workspace=project,
)
if result_only_evidence.sequence != ["open_workspace", "read"]:
    raise SystemExit(
        "current ChatGPT result-only read shape was not recognized: "
        f"{result_only_evidence}"
    )
local_window = [
    {
        "tool": "open_workspace",
        "success": True,
        "path": str(project),
        "mode": "checkout",
        "workspace_id": "test",
        "result_workspace_id": "test",
        "result_root": str(project),
    },
    {
        "tool": "read",
        "success": True,
        "workspace_id": "test",
        "path": "README.md",
    },
    {
        "tool": "read",
        "success": True,
        "workspace_id": "unrelated",
        "path": "OTHER.md",
    },
]
attributed_records = advisor_agent.records_for_workspace(local_window, "test")
attributed_evidence = advisor_agent.summarize_tool_evidence(
    attributed_records,
    allow_shell=False,
    expected_workspace=project,
)
validation_errors = advisor_agent.validate_result(
    returncode=0,
    output="Verified.\nADVISOR-AGENT-TEST-COMPLETE",
    marker="ADVISOR-AGENT-TEST-COMPLETE",
    evidence=result_only_evidence,
    min_inspection_calls=1,
    require_tool_activity=True,
    corroborating_evidence=attributed_evidence,
)
if validation_errors:
    raise SystemExit(
        "result-only conversation evidence was not corroborated by workspace id: "
        + "; ".join(validation_errors)
    )
local_window[1]["path"] = ".env"
denied_evidence = advisor_agent.summarize_tool_evidence(
    advisor_agent.records_for_workspace(local_window, "test"),
    allow_shell=False,
    expected_workspace=project,
)
denied_errors = advisor_agent.validate_result(
    returncode=0,
    output="Verified.\nADVISOR-AGENT-TEST-COMPLETE",
    marker="ADVISOR-AGENT-TEST-COMPLETE",
    evidence=result_only_evidence,
    min_inspection_calls=1,
    require_tool_activity=True,
    corroborating_evidence=denied_evidence,
)
if not any("denied or escaping path" in error for error in denied_errors):
    raise SystemExit("workspace-id corroboration did not reject a sensitive local path")

blocked = {
    "call": {
        "children": [],
        "message": {
            "recipient": "api_tool.call_tool",
            "content": {
                "text": json.dumps({"path": "mcp://devspace/open_workspace", "args": {}})
            },
        },
    }
}
try:
    advisor.load_chatgpt_auth = lambda: {"headers": {}}
    advisor.get_json = lambda *_args, **_kwargs: {"mapping": blocked}
    records, error = advisor_agent.remote_tool_records(state_path, 5)
finally:
    advisor.load_chatgpt_auth = old_auth
    advisor.get_json = old_get
blocked_evidence = advisor_agent.summarize_tool_evidence(records, allow_shell=False)
if error or blocked_evidence.failed != ["open_workspace"]:
    raise SystemExit("blocked MCP call was not attributed as failed")

stale_prompt = "Inspect the current turn only."
stale_conversation = {
    "current_node": "current-final",
    "mapping": {
        "old-user": {
            "id": "old-user",
            "parent": None,
            "children": ["old-call"],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["Old prompt."]},
            },
        },
        "old-call": {
            "id": "old-call",
            "parent": "old-user",
            "children": ["old-result"],
            "message": {
                "recipient": "api_tool.call_tool",
                "content": {
                    "text": json.dumps({
                        "path": "mcp://devspace/open_workspace",
                        "args": {"path": str(project), "mode": "checkout"},
                    })
                },
            },
        },
        "old-result": {
            "id": "old-result",
            "parent": "old-call",
            "children": ["current-user"],
            "message": {
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "status": "finished_successfully",
                "content": {"text": json.dumps({
                    "workspaceId": "old",
                    "root": str(project),
                    "mode": "checkout",
                })},
                "metadata": {"invoked_resource": {"resource_uri": "mcp://devspace/open_workspace"}},
            },
        },
        "current-user": {
            "id": "current-user",
            "parent": "old-result",
            "children": ["current-final"],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": [stale_prompt]},
            },
        },
        "current-final": {
            "id": "current-final",
            "parent": "current-user",
            "children": [],
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["Unverified final."]},
                "end_turn": True,
                "status": "finished_successfully",
            },
        },
    },
}
if advisor_agent.tool_records_from_conversation_data(stale_conversation, stale_prompt):
    raise SystemExit("stale prior-turn tool evidence was accepted for the current prompt")

wrong_records = [
    {
        "tool": "open_workspace",
        "success": True,
        "path": str(project.parent),
        "mode": "checkout",
        "result_workspace_id": "wrong",
        "result_root": str(project.parent),
    },
    {
        "tool": "read",
        "success": True,
        "workspace_id": "wrong",
        "path": ".env",
    },
    {
        "tool": "open_workspace",
        "success": True,
        "path": str(project),
        "mode": "checkout",
        "result_workspace_id": "right",
        "result_root": str(project),
    },
]
wrong_evidence = advisor_agent.summarize_tool_evidence(
    wrong_records,
    allow_shell=False,
    expected_workspace=project,
)
if (
    wrong_evidence.attempted_open_workspace_count != 2
    or wrong_evidence.wrong_workspace_open_count < 1
    or wrong_evidence.sensitive_path_attempt_count != 1
):
    raise SystemExit(f"wrong-path or duplicate-open evidence was not rejected: {wrong_evidence}")

if not advisor_agent.tool_result_has_error({"isError": True}):
    raise SystemExit("MCP isError result was not rejected")
if not advisor_agent.tool_result_has_error({"result": {"success": False}}):
    raise SystemExit("nested failed MCP result was not rejected")

prompt = "Inspect one file and return the final report."
conversation = {
    "current_node": "final",
    "mapping": {
        "user": {
            "id": "user",
            "parent": None,
            "children": ["progress"],
            "message": {
                "id": "user-message",
                "author": {"role": "user"},
                "content": {"parts": [prompt]},
            },
        },
        "progress": {
            "id": "progress",
            "parent": "user",
            "children": ["final"],
            "message": {
                "id": "progress-message",
                "author": {"role": "assistant"},
                "content": {"parts": ["I am still inspecting."]},
                "end_turn": False,
                "status": "finished_successfully",
            },
        },
        "final": {
            "id": "final",
            "parent": "progress",
            "children": [],
            "message": {
                "id": "final-message",
                "author": {"role": "assistant"},
                "content": {"parts": ["Final verified report."]},
                "end_turn": True,
                "status": "finished_successfully",
            },
        },
    },
}
final_text = advisor_agent.final_text_from_conversation_data(conversation, prompt)
if final_text != "Final verified report.":
    raise SystemExit(f"agent final response was not isolated: {final_text!r}")
cleaned = advisor_agent.strip_completion_marker(
    "Final verified report.\n\nADVISOR-AGENT-TEST-COMPLETE",
    "ADVISOR-AGENT-TEST-COMPLETE",
)
if cleaned != "Final verified report.":
    raise SystemExit(f"agent completion marker was not stripped: {cleaned!r}")

if agent_conclave.safe_response_path(project, project.parent / "outside.md") is not None:
    raise SystemExit("agent_conclave accepted a response path outside the project")

args = type(
    "Args",
    (),
    {
        "project_dir": project,
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "timeout": 900,
        "queue_timeout": 3600.0,
        "max_output_tokens": 1600,
        "model": None,
        "thinking_effort": None,
        "allow_shell": False,
        "live_activity": True,
    },
)()
command = agent_conclave.role_command(args, "critic")
if command[command.index("--queue-timeout") + 1] != "3600.0":
    raise SystemExit("agent_conclave did not propagate the worker queue timeout")

print("Agent-conclave helper tests passed.")
PY

echo "Agent-conclave tests passed."
