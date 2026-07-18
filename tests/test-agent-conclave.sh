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
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path

import advisor
import advisor_agent
import advisor_concurrency
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

graph_retry_records = [
    {
        "tool": "open_workspace",
        "success": False,
        "path": str(project),
        "mode": "checkout",
        "workspace_id": None,
        "result_workspace_id": None,
        "result_root": None,
        "result_only": False,
    },
    local_window[0],
    local_window[1],
]
graph_retry_evidence = advisor_agent.summarize_tool_evidence(
    graph_retry_records,
    allow_shell=False,
    expected_workspace=project,
)
retry_errors = advisor_agent.validate_result(
    returncode=0,
    output="Verified.\nADVISOR-AGENT-TEST-COMPLETE",
    marker="ADVISOR-AGENT-TEST-COMPLETE",
    evidence=graph_retry_evidence,
    min_inspection_calls=1,
    require_tool_activity=True,
    corroborating_evidence=attributed_evidence,
)
if retry_errors:
    raise SystemExit(
        "a graph-only failed open retry overrode corroborated private evidence: "
        + "; ".join(retry_errors)
    )

duplicate_local_evidence = advisor_agent.summarize_tool_evidence(
    [local_window[0], local_window[0], local_window[1]],
    allow_shell=False,
    expected_workspace=project,
)
duplicate_errors = advisor_agent.validate_result(
    returncode=0,
    output="Verified.\nADVISOR-AGENT-TEST-COMPLETE",
    marker="ADVISOR-AGENT-TEST-COMPLETE",
    evidence=graph_retry_evidence,
    min_inspection_calls=1,
    require_tool_activity=True,
    corroborating_evidence=duplicate_local_evidence,
)
if not any("private DevSpace log" in error for error in duplicate_errors):
    raise SystemExit("multiple real open_workspace calls were not rejected")

unknown_result = {
    "mapping": {
        "unknown-result": {
            "parent": None,
            "message": {
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "status": "finished_successfully",
                "content": {"text": "{}"},
                "metadata": {
                    "invoked_resource": {"resource_uri": "mcp://devspace/delete"}
                },
            },
        }
    }
}
unknown_records = advisor_agent.tool_records_from_conversation_data(unknown_result)
unknown_evidence = advisor_agent.summarize_tool_evidence(
    unknown_records,
    allow_shell=False,
)
if unknown_evidence.sequence != ["unknown"] or unknown_evidence.disallowed != ["unknown"]:
    raise SystemExit("an unknown result-only tool was hidden instead of failing closed")

private_log = project / "private-tools.jsonl"
private_log.write_text(
    json.dumps({
        "event": "tool_call",
        "tool": "future_mutation_tool",
        "success": True,
        "workspaceId": "test",
        "path": "README.md",
    }) + "\n",
    encoding="utf-8",
)
private_unknown = advisor_agent.summarize_tool_evidence(
    advisor_agent.read_tool_records(private_log),
    allow_shell=False,
)
if private_unknown.sequence != ["unknown"] or private_unknown.disallowed != ["unknown"]:
    raise SystemExit("an unknown private-log tool was discarded instead of failing closed")

for selector_record in (
    {
        "tool": "glob",
        "success": True,
        "workspace_id": "test",
        "path": ".",
        "selectors": [".", "../**/*.py"],
    },
    {
        "tool": "grep",
        "success": True,
        "workspace_id": "test",
        "path": ".",
        "selectors": [".", "C:\\Users\\owner\\.env"],
    },
    {
        "tool": "read",
        "success": True,
        "workspace_id": "test",
        "path": "\\\\server\\share\\secret.pem",
    },
    {
        "tool": "read",
        "success": True,
        "workspace_id": "test",
        "path": "C:relative-secret.env",
    },
    {
        "tool": "read",
        "success": True,
        "workspace_id": "test",
        "path": "file://outside/secret.pem",
    },
):
    selector_evidence = advisor_agent.summarize_tool_evidence(
        [local_window[0], selector_record],
        allow_shell=False,
        expected_workspace=project,
    )
    if selector_evidence.sensitive_path_attempt_count != 1:
        raise SystemExit(f"escaping path selector was not rejected: {selector_record}")

for denied_pattern in (".env*", ".e[n]v", "{README.md,.env}", "**/*.pem", "config/{safe,secret.key}"):
    if not advisor_agent.tool_path_is_sensitive(denied_pattern):
        raise SystemExit(f"wildcard selector could match a denied path: {denied_pattern}")
for allowed_pattern in ("*.py", "src/**/*.ts", "README*.md"):
    if advisor_agent.tool_path_is_sensitive(allowed_pattern):
        raise SystemExit(f"benign wildcard selector was rejected: {allowed_pattern}")

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
    or wrong_evidence.sensitive_path_attempt_count < 1
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

unfinished_prompt = "This submitted turn is still running."
unrelated_conversation = {
    "current_node": "later-final",
    "mapping": {
        "unfinished-user": {
            "id": "unfinished-user",
            "parent": None,
            "children": ["unfinished-progress"],
            "message": {
                "id": "unfinished-user-message",
                "author": {"role": "user"},
                "content": {"parts": [unfinished_prompt]},
            },
        },
        "unfinished-progress": {
            "id": "unfinished-progress",
            "parent": "unfinished-user",
            "children": ["later-user"],
            "message": {
                "id": "unfinished-progress-message",
                "author": {"role": "assistant"},
                "content": {"parts": ["Still working."]},
                "end_turn": False,
                "status": "in_progress",
            },
        },
        "later-user": {
            "id": "later-user",
            "parent": "unfinished-progress",
            "children": ["later-final"],
            "message": {
                "id": "later-user-message",
                "author": {"role": "user"},
                "content": {"parts": ["Answer a different question."]},
            },
        },
        "later-final": {
            "id": "later-final",
            "parent": "later-user",
            "children": [],
            "message": {
                "id": "later-final-message",
                "author": {"role": "assistant"},
                "content": {"parts": ["Unrelated finished answer."]},
                "end_turn": True,
                "status": "finished_successfully",
            },
        },
    },
}
unrelated_text = advisor_agent.final_text_from_conversation_data(
    unrelated_conversation,
    unfinished_prompt,
)
if unrelated_text:
    raise SystemExit(
        "agent recovery accepted a later unrelated assistant response: "
        f"{unrelated_text!r}"
    )
cleaned = advisor_agent.strip_completion_marker(
    "Final verified report.\n\nADVISOR-AGENT-TEST-COMPLETE",
    "ADVISOR-AGENT-TEST-COMPLETE",
)
if cleaned != "Final verified report.":
    raise SystemExit(f"agent completion marker was not stripped: {cleaned!r}")

if agent_conclave.safe_response_path(project, project.parent / "outside.md") is not None:
    raise SystemExit("agent_conclave accepted a response path outside the project")

resume_root = project / ".codex-advisor" / "agent-conclave-runs" / "resume-test" / "roles"
resume_dir = resume_root / "critic"
resume_dir.mkdir(parents=True)
resume_marker = "ADVISOR-AGENT-RESUME-TEST-COMPLETE"
resume_prompt = f"Inspect the repository.\n\nFinish with:\n{resume_marker}"
resume_log = resume_dir / "devspace-tools.jsonl"
resume_log.write_text(
    "\n".join(
        [
            json.dumps({
                "event": "tool_call",
                "tool": "open_workspace",
                "success": True,
                "workspaceId": "resume-workspace",
                "path": str(project),
            }),
            json.dumps({
                "event": "tool_call",
                "tool": "read",
                "success": True,
                "workspaceId": "resume-workspace",
                "path": "README.md",
            }),
        ]
    ) + "\n",
    encoding="utf-8",
)
resume_state = resume_dir / "conversation.json"
resume_request = {
    "project_dir": str(project),
    "workspace_dir": str(project),
    "role": "critic",
    "task": "Inspect the repository.",
    "marker": resume_marker,
    "prompt": resume_prompt,
    "state_path": str(resume_state),
    "journal_path": str(resume_dir / "turn-journal.json"),
    "log_path": str(resume_log),
    "chatgpt_project_id": "project-test",
    "provider": "openai-compatible",
    "model": "gpt-5-6-thinking",
    "thinking_effort": "max",
    "min_inspection_calls": 1,
    "require_tool_activity": True,
}
(resume_dir / "request.json").write_text(json.dumps(resume_request), encoding="utf-8")
(resume_dir / "turn-journal.json").write_text(
    json.dumps({"phase": "submission-started"}),
    encoding="utf-8",
)
resume_conversation = {
    "conversation_id": "resume-conversation",
    "current_node": "final",
    "mapping": {
        "user": {
            "parent": None,
            "children": ["open-call"],
            "message": {
                "id": "user",
                "author": {"role": "user"},
                "content": {"parts": [resume_prompt]},
                "status": "finished_successfully",
            },
        },
        "open-call": {
            "parent": "user",
            "children": ["open-result"],
            "message": {
                "recipient": "api_tool.call_tool",
                "content": {"text": json.dumps({
                    "path": "mcp://devspace/open_workspace",
                    "args": {"path": str(project), "mode": "checkout"},
                })},
            },
        },
        "open-result": {
            "parent": "open-call",
            "children": ["read-call"],
            "message": {
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "status": "finished_successfully",
                "content": {"text": json.dumps({
                    "workspaceId": "resume-workspace",
                    "root": str(project),
                })},
                "metadata": {"invoked_resource": {"resource_uri": "mcp://devspace/open_workspace"}},
            },
        },
        "read-call": {
            "parent": "open-result",
            "children": ["read-result"],
            "message": {
                "recipient": "api_tool.call_tool",
                "content": {"text": json.dumps({
                    "path": "mcp://devspace/read",
                    "args": {"workspaceId": "resume-workspace", "path": "README.md"},
                })},
            },
        },
        "read-result": {
            "parent": "read-call",
            "children": ["final"],
            "message": {
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "status": "finished_successfully",
                "content": {"text": json.dumps({"result": "verified"})},
                "metadata": {"invoked_resource": {"resource_uri": "mcp://devspace/read"}},
            },
        },
        "final": {
            "parent": "read-result",
            "children": [],
            "message": {
                "id": "final",
                "author": {"role": "assistant"},
                "content": {"parts": [f"Recovered report.\n{resume_marker}"]},
                "status": "finished_successfully",
                "end_turn": True,
            },
        },
    },
}
resume_args = type(
    "ResumeArgs",
    (),
    {
        "project_dir": project,
        "resume_run_dir": resume_dir,
        "timeout": 5,
        "json": True,
    },
)()
old_discover = advisor_agent.discover_exact_remote_conversation
old_load_auth = advisor.load_chatgpt_auth
discover_calls = {"count": 0}
try:
    def recover_once(*_args, **_kwargs):
        discover_calls["count"] += 1
        return resume_conversation, "resume-conversation", ""

    advisor_agent.discover_exact_remote_conversation = recover_once
    advisor.load_chatgpt_auth = lambda: {"headers": {}, "user_id": "test-user"}
    with contextlib.redirect_stdout(io.StringIO()):
        resume_code = advisor_agent.resume_agent_run(resume_args, resume_request)
finally:
    advisor_agent.discover_exact_remote_conversation = old_discover
    advisor.load_chatgpt_auth = old_load_auth
if resume_code != 0 or discover_calls["count"] != 1:
    raise SystemExit("interrupted role was not recovered with exactly one read-only discovery pass")
resume_meta = json.loads((resume_dir / "meta.json").read_text(encoding="utf-8"))
if resume_meta.get("status") != "ok" or (resume_dir / "response.md").read_text(encoding="utf-8").strip() != "Recovered report.":
    raise SystemExit(f"interrupted role recovery did not persist a verified final: {resume_meta!r}")

unsubmitted_dir = resume_root / "planner"
unsubmitted_dir.mkdir()
unsubmitted_request = {
    **resume_request,
    "role": "planner",
    "state_path": str(unsubmitted_dir / "conversation.json"),
    "journal_path": str(unsubmitted_dir / "turn-journal.json"),
    "log_path": str(resume_log),
}
(unsubmitted_dir / "request.json").write_text(json.dumps(unsubmitted_request), encoding="utf-8")
unsubmitted_args = type(
    "ResumeArgs",
    (),
    {
        "project_dir": project,
        "resume_run_dir": unsubmitted_dir,
        "timeout": 5,
        "json": True,
    },
)()
with contextlib.redirect_stdout(io.StringIO()):
    unsubmitted_code = advisor_agent.resume_agent_run(unsubmitted_args, unsubmitted_request)
unsubmitted_meta = json.loads((unsubmitted_dir / "meta.json").read_text(encoding="utf-8"))
if unsubmitted_code != 3 or unsubmitted_meta.get("status") != "not-submitted" or not unsubmitted_meta.get("safe_to_submit"):
    raise SystemExit("an unsubmitted interrupted role was not distinguished from an ambiguous submitted turn")

synthesis_dir = project / ".codex-advisor" / "agent-conclave-runs" / "synthesis-resume-test"
synthesis_dir.mkdir(parents=True)
synthesis_task = "Synthesize recovered roles."
synthesis_roles = [
    agent_conclave.AgentRoleResult(
        role="critic",
        ok=True,
        output="Recovered report.",
        elapsed_seconds=1.0,
        metadata={"status": "ok"},
    )
]
synthesis_checkpoint, synthesis_input_sha = agent_conclave.synthesis_checkpoint_dir(
    synthesis_dir,
    synthesis_task,
    synthesis_roles,
)
synthesis_marker = "ADVISOR-SYNTHESIS-RESUME-TEST-COMPLETE"
synthesis_prompt = agent_conclave.synthesis_prompt(
    synthesis_task,
    synthesis_roles,
    synthesis_marker,
)
synthesis_state = synthesis_checkpoint / "conversation.json"
(synthesis_checkpoint / "request.json").write_text(
    json.dumps({
        "project_dir": str(project),
        "input_sha256": synthesis_input_sha,
        "checkpoint_dir": str(synthesis_checkpoint),
        "prompt": synthesis_prompt,
        "marker": synthesis_marker,
        "state_path": str(synthesis_state),
        "journal_path": str(synthesis_checkpoint / "turn-journal.json"),
        "response_path": str(synthesis_checkpoint / "response.md"),
        "chatgpt_project_id": "project-test",
    }),
    encoding="utf-8",
)
(synthesis_checkpoint / "turn-journal.json").write_text(
    json.dumps({"phase": "submission-outcome-unknown"}),
    encoding="utf-8",
)
synthesis_conversation = {
    "conversation_id": "synthesis-conversation",
    "current_node": "final",
    "mapping": {
        "user": {
            "parent": None,
            "children": ["final"],
            "message": {
                "id": "user",
                "author": {"role": "user"},
                "content": {"parts": [synthesis_prompt]},
                "status": "finished_successfully",
            },
        },
        "final": {
            "parent": "user",
            "children": [],
            "message": {
                "id": "final",
                "author": {"role": "assistant"},
                "content": {"parts": [f"Recovered synthesis.\n{synthesis_marker}"]},
                "status": "finished_successfully",
                "end_turn": True,
            },
        },
    },
}
old_discover = advisor_agent.discover_exact_remote_conversation
old_load_auth = advisor.load_chatgpt_auth
synthesis_discover_calls = {"count": 0}
try:
    def recover_synthesis_once(*_args, **_kwargs):
        synthesis_discover_calls["count"] += 1
        return synthesis_conversation, "synthesis-conversation", ""

    advisor_agent.discover_exact_remote_conversation = recover_synthesis_once
    advisor.load_chatgpt_auth = lambda: {"headers": {}, "user_id": "test-user"}
    synthesis_status, synthesis_output, synthesis_error = agent_conclave.recover_synthesis(
        resume_args,
        synthesis_dir,
        synthesis_task,
        synthesis_roles,
    )
finally:
    advisor_agent.discover_exact_remote_conversation = old_discover
    advisor.load_chatgpt_auth = old_load_auth
if (
    synthesis_status != "ok"
    or synthesis_output != "Recovered synthesis."
    or synthesis_error
    or synthesis_discover_calls["count"] != 1
):
    raise SystemExit("interrupted synthesis was not recovered with exactly one GET-only discovery pass")
if (synthesis_dir / "synthesis.md").read_text(encoding="utf-8").strip() != "Recovered synthesis.":
    raise SystemExit("recovered synthesis was not persisted")

safe_synthesis_dir = project / ".codex-advisor" / "agent-conclave-runs" / "synthesis-unsubmitted-test"
safe_synthesis_dir.mkdir(parents=True)
safe_checkpoint, safe_input_sha = agent_conclave.synthesis_checkpoint_dir(
    safe_synthesis_dir,
    synthesis_task,
    synthesis_roles,
)
(safe_checkpoint / "request.json").write_text(
    json.dumps({
        "project_dir": str(project),
        "input_sha256": safe_input_sha,
        "checkpoint_dir": str(safe_checkpoint),
        "prompt": synthesis_prompt,
        "marker": synthesis_marker,
        "state_path": str(safe_checkpoint / "conversation.json"),
        "journal_path": str(safe_checkpoint / "turn-journal.json"),
        "chatgpt_project_id": "project-test",
    }),
    encoding="utf-8",
)
safe_status, _, _ = agent_conclave.recover_synthesis(
    resume_args,
    safe_synthesis_dir,
    synthesis_task,
    synthesis_roles,
)
if safe_status != "safe-to-submit":
    raise SystemExit("unsubmitted synthesis was not distinguished from an ambiguous submitted synthesis")

expanded_roles = [
    *synthesis_roles,
    agent_conclave.AgentRoleResult(
        role="planner",
        ok=True,
        output="Late recovered planner report.",
        elapsed_seconds=2.0,
        metadata={"status": "ok"},
    ),
]
expanded_status, _, _ = agent_conclave.recover_synthesis(
    resume_args,
    synthesis_dir,
    synthesis_task,
    expanded_roles,
)
if expanded_status != "safe-to-submit":
    raise SystemExit("newly recovered role evidence incorrectly reused an older partial synthesis")

locked_run = project / ".codex-advisor" / "agent-conclave-runs" / "run-lock-test"
locked_role = locked_run / "roles" / "critic"
locked_role.mkdir(parents=True)
(locked_run / "manifest.json").write_text(
    json.dumps({
        "schema_version": "2.0",
        "project_dir": str(project),
        "task": "Test duplicate resume exclusion.",
        "mode": "code-review",
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "roles": ["critic"],
        "role_runs": {
            "critic": {
                "run_dir": str(locked_role),
                "recovery_token": "ADVISOR-AGENT-RUN-LOCK-TEST-COMPLETE",
                "status": "pending",
            }
        },
        "parallel": False,
        "max_workers": 1,
        "request_timeout_seconds": 0,
        "queue_timeout_seconds": 0,
        "max_output_tokens": 1600,
        "allow_partial": False,
        "no_synthesis": True,
        "live_activity": False,
    }),
    encoding="utf-8",
)
with advisor_concurrency.InterProcessLock(locked_run / "run.lock", timeout=1.0):
    locked_result = subprocess.run(
        [sys.executable, agent_conclave.__file__, "--resume-run", str(locked_run)],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
if locked_result.returncode != 2 or "run lock failed" not in locked_result.stderr:
    raise SystemExit("a second process was not blocked from reconciling the same conclave run")

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
command = agent_conclave.role_command(
    args,
    "critic",
    {
        "run_dir": str(project / ".codex-advisor" / "role"),
        "recovery_token": "ADVISOR-AGENT-TEST-COMPLETE",
    },
    resume=False,
)
if command[command.index("--queue-timeout") + 1] != "3600.0":
    raise SystemExit("agent_conclave did not propagate the worker queue timeout")
if agent_conclave.combined_subprocess_timeout(0, 0, 60) is not None:
    raise SystemExit("agent_conclave imposed an outer timeout on unlimited roles")
if advisor_agent.combined_subprocess_timeout(0, 0, 30) is not None:
    raise SystemExit("advisor_agent imposed an outer timeout on an unlimited remote turn")

saved_argv = sys.argv[:]
saved_env = {name: os.environ.pop(name, None) for name in ("ADVISOR_AGENT_TIMEOUT", "ADVISOR_QUEUE_TIMEOUT")}
try:
    sys.argv = ["agent_conclave.py", "--prompt", "test", "--dry-run"]
    defaults = agent_conclave.parse_args()
    if (defaults.timeout, defaults.queue_timeout, defaults.max_workers) != (0, 0.0, 5):
        raise SystemExit("agent_conclave no longer defaults to unlimited five-role execution")
    sys.argv = ["advisor_agent.py", "--prompt", "test", "--dry-run"]
    agent_defaults = advisor_agent.parse_args()
    if (agent_defaults.timeout, agent_defaults.queue_timeout) != (0, 0.0):
        raise SystemExit("advisor_agent no longer defaults to unlimited completion waiting")
finally:
    sys.argv = saved_argv
    for name, value in saved_env.items():
        if value is not None:
            os.environ[name] = value

print("Agent-conclave helper tests passed.")
PY

if python3 "$SCRIPTS/advisor_agent.py" \
  --project-dir "$PROJECT" \
  --base-url "https://example.invalid/v1" \
  --dry-run \
  --prompt "Do not send this." >/dev/null 2>"$PROJECT/remote-agent.err"; then
  echo "advisor_agent accepted a non-loopback repo-aware endpoint" >&2
  exit 1
fi
grep -q "requires a loopback" "$PROJECT/remote-agent.err"

if python3 "$SCRIPTS/agent_conclave.py" \
  --project-dir "$PROJECT" \
  --base-url "https://example.invalid/v1" \
  --dry-run \
  --prompt "Do not send this." >/dev/null 2>"$PROJECT/remote-conclave.err"; then
  echo "agent_conclave accepted a non-loopback repo-aware endpoint" >&2
  exit 1
fi
grep -q "requires a loopback" "$PROJECT/remote-conclave.err"

echo "Agent-conclave tests passed."
