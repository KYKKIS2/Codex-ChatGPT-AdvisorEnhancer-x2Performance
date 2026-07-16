#!/usr/bin/env python3
"""Run one bounded repo-aware ChatGPT advisor through the registered DevSpace MCP."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import activity_monitor
import advisor_agent_connect
import advisor_safety as safety
import agent_mode


INSPECTION_TOOLS = {"read", "grep", "glob", "ls"}
SHELL_TOOLS = {"bash", "exec_command", "write_stdin"}
MUTATION_TOOLS = {"write", "edit", "apply_patch", "show_changes"}
SAFE_TOOL_NAMES = {"open_workspace", *INSPECTION_TOOLS, *SHELL_TOOLS, *MUTATION_TOOLS}
DEFAULT_MODEL = "gpt-5-6-thinking"
DEFAULT_TIMEOUT = 900
DEFAULT_QUEUE_TIMEOUT = 3600.0


@dataclass
class ToolEvidence:
    total: int
    sequence: list[str]
    successful: list[str]
    failed: list[str]
    disallowed: list[str]
    attempted_open_workspace_count: int
    open_workspace_count: int
    inspection_count: int
    wrong_workspace_open_count: int
    inspection_before_open_count: int
    workspace_id_mismatch_count: int
    sensitive_path_attempt_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "sequence": self.sequence,
            "successful": self.successful,
            "failed": self.failed,
            "disallowed": self.disallowed,
            "attempted_open_workspace_count": self.attempted_open_workspace_count,
            "open_workspace_count": self.open_workspace_count,
            "inspection_count": self.inspection_count,
            "wrong_workspace_open_count": self.wrong_workspace_open_count,
            "inspection_before_open_count": self.inspection_before_open_count,
            "workspace_id_mismatch_count": self.workspace_id_mismatch_count,
            "sensitive_path_attempt_count": self.sensitive_path_attempt_count,
        }


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_project(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.expanduser().resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def private_run_dir(project: Path, role: str) -> Path:
    root = project / ".codex-advisor" / "agent-runs"
    safety.ensure_private_dir(root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / f"{stamp}-{uuid.uuid4().hex[:8]}-{safety.safe_slug(role, default='reviewer')}"
    safety.ensure_private_dir(run_dir)
    return run_dir


def connector_status(project: Path) -> dict[str, Any]:
    root = advisor_agent_connect.runtime_root()
    return advisor_agent_connect.connector_runtime_status(project, root=root)


def refresh_agent_workspace(project: Path) -> tuple[Path, dict[str, Any]]:
    roots = agent_mode.configured_allowed_roots()
    status = agent_mode.evaluate_agent_mode(
        project,
        mode="on",
        allowed_roots=roots,
        bridge_executable=os.environ.get(
            agent_mode.BRIDGE_EXECUTABLE_ENV,
            agent_mode.DEFAULT_BRIDGE_EXECUTABLE,
        ),
        sanitized_workspace_mode=os.environ.get(
            agent_mode.SANITIZED_WORKSPACE_ENV,
            agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE,
        ),
    )
    if not status.available:
        details = "; ".join(status.errors[:6]) or "agent-mode safety preflight failed"
        raise RuntimeError(details)
    workspace = Path(status.project_dir).expanduser().resolve()
    return workspace, status.to_dict()


def validate_workspace_for_connector(workspace: Path, state: dict[str, Any]) -> None:
    raw_root = state.get("allowed_root")
    if not isinstance(raw_root, str) or not raw_root:
        raise RuntimeError("The live connector has no recorded allowed root.")
    allowed_root = Path(raw_root).expanduser().resolve()
    if not agent_mode.path_is_same_or_child(workspace, allowed_root):
        raise RuntimeError(
            "The refreshed review workspace is outside the live connector allowed root. "
            "Restart the connector for this project, then update the ChatGPT app URL only if the tunnel changed."
        )


def read_tool_records(log_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("event") != "tool_call":
            continue
        tool = record.get("tool")
        success = record.get("success")
        if not isinstance(tool, str) or tool not in SAFE_TOOL_NAMES or not isinstance(success, bool):
            continue
        workspace_id = record.get("workspaceId")
        path = record.get("path")
        item = {
            "tool": tool,
            "success": success,
            "workspace_id": workspace_id if isinstance(workspace_id, str) else "",
            "path": path if isinstance(path, str) else None,
            "source": "project-log",
        }
        if tool == "open_workspace":
            item.update(
                {
                    "mode": "checkout",
                    "result_workspace_id": item["workspace_id"],
                    "result_root": item["path"],
                }
            )
        records.append(item)
    return records


def summarize_tool_evidence(
    records: list[dict[str, Any]],
    *,
    allow_shell: bool,
    expected_workspace: Path | None = None,
) -> ToolEvidence:
    allowed = {"open_workspace", *INSPECTION_TOOLS}
    if allow_shell:
        allowed.update(SHELL_TOOLS)
    sequence = [str(record["tool"]) for record in records]
    successful = [str(record["tool"]) for record in records if record["success"]]
    failed = [str(record["tool"]) for record in records if not record["success"]]
    disallowed = [tool for tool in sequence if tool not in allowed]
    expected = expected_workspace.expanduser().resolve() if expected_workspace is not None else None
    attempted_open_workspace_count = sequence.count("open_workspace")
    wrong_workspace_open_count = 0
    inspection_before_open_count = 0
    workspace_id_mismatch_count = 0
    sensitive_path_attempt_count = 0
    active_workspace_id = ""
    workspace_opened = False
    for record in records:
        tool = str(record.get("tool") or "unknown")
        if tool == "open_workspace" and record.get("success"):
            raw_path = record.get("path")
            result_root = record.get("result_root")
            mode = record.get("mode")
            if expected is not None:
                try:
                    opened_path = Path(str(raw_path)).expanduser().resolve()
                except (OSError, TypeError, ValueError):
                    opened_path = Path()
                try:
                    returned_root = Path(str(result_root)).expanduser().resolve()
                except (OSError, TypeError, ValueError):
                    returned_root = Path()
                if opened_path != expected or returned_root != expected or mode not in (None, "", "checkout"):
                    wrong_workspace_open_count += 1
            active_workspace_id = str(record.get("result_workspace_id") or "")
            if not active_workspace_id:
                wrong_workspace_open_count += 1
            workspace_opened = True
            continue
        if tool not in INSPECTION_TOOLS:
            continue
        if not workspace_opened:
            inspection_before_open_count += 1
        if record.get("result_only"):
            continue
        if workspace_opened and str(record.get("workspace_id") or "") != active_workspace_id:
            workspace_id_mismatch_count += 1
        if tool_path_is_sensitive(record.get("path")):
            sensitive_path_attempt_count += 1
    return ToolEvidence(
        total=len(records),
        sequence=sequence,
        successful=successful,
        failed=failed,
        disallowed=disallowed,
        attempted_open_workspace_count=attempted_open_workspace_count,
        open_workspace_count=successful.count("open_workspace"),
        inspection_count=sum(tool in INSPECTION_TOOLS for tool in successful),
        wrong_workspace_open_count=wrong_workspace_open_count,
        inspection_before_open_count=inspection_before_open_count,
        workspace_id_mismatch_count=workspace_id_mismatch_count,
        sensitive_path_attempt_count=sensitive_path_attempt_count,
    )


def tool_name_from_call_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        return "unknown"
    candidate = re.split(r"[/.:]", raw_path.strip())[-1]
    return candidate if candidate in SAFE_TOOL_NAMES else "unknown"


DENIED_INSPECTION_PATH_PARTS = {
    ".codex-advisor",
    ".git",
    ".ssh",
    "har_and_cookies",
    "wallets",
}


def tool_path_is_sensitive(raw_path: Any) -> bool:
    if raw_path in (None, ""):
        return False
    if not isinstance(raw_path, str):
        return True
    normalized = raw_path.replace("\\", "/").strip()
    if not normalized or normalized.startswith(("/", "~")):
        return True
    parts = [part.lower() for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts or any(part in DENIED_INSPECTION_PATH_PARTS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name == ".env" or name.startswith(".env."):
        return True
    if name.startswith("auth_") and name.endswith(".json"):
        return True
    return any(name.endswith(suffix) for suffix in safety.SENSITIVE_SUFFIXES)


def tool_result_has_error(value: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("success") is False:
            return True
        if value.get("error") not in (None, "", False):
            return True
        return any(tool_result_has_error(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(tool_result_has_error(item, depth + 1) for item in value)
    return False


def tool_result_succeeded(message: dict[str, Any]) -> bool:
    if message.get("status") != "finished_successfully":
        return False
    content = message.get("content")
    raw_text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(raw_text, str):
        return True
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return True
    return not tool_result_has_error(parsed)


def remote_conversation_data(state_path: Path, timeout: int) -> tuple[dict[str, Any], str]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"could not read saved advisor conversation state: {exc}"
    conversation = state.get("conversation")
    conversation_id = conversation.get("conversation_id") if isinstance(conversation, dict) else None
    if not isinstance(conversation_id, str) or not conversation_id:
        return {}, "saved advisor state has no ChatGPT conversation id"

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    auth = advisor.load_chatgpt_auth()
    if not auth:
        return {}, "ChatGPT HAR/auth is unavailable for per-conversation MCP verification"
    try:
        data = advisor.get_json(
            f"https://chatgpt.com/backend-api/conversation/{conversation_id}",
            auth["headers"],
            min(max(timeout, 1), 60),
        )
    except RuntimeError as exc:
        return {}, "could not verify per-conversation MCP activity: " + safety.redact_sensitive_text(str(exc))

    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return {}, "ChatGPT conversation response has no message mapping"
    return data, ""


def ordered_active_node_items(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return []
    current = data.get("current_node") or data.get("current_node_id")
    if isinstance(current, str) and current:
        items: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            node = mapping.get(current)
            if not isinstance(node, dict):
                break
            items.append((current, node))
            parent = node.get("parent")
            current = parent if isinstance(parent, str) else ""
        items.reverse()
        return items
    items = [
        (str(node_id), node)
        for node_id, node in mapping.items()
        if isinstance(node, dict)
    ]
    items.sort(
        key=lambda item: (
            (item[1].get("message") or {}).get("create_time") or 0,
            item[0],
        )
    )
    return items


def conversation_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    raw_text = content.get("text")
    if isinstance(raw_text, str):
        return raw_text
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "\n".join(part for part in parts if isinstance(part, str))


def current_turn_node_items(
    data: dict[str, Any],
    prompt: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    items = ordered_active_node_items(data)
    if not prompt:
        return items
    normalized_prompt = prompt.strip()
    prompt_index = -1
    for index, (_node_id, node) in enumerate(items):
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        if (
            isinstance(author, dict)
            and author.get("role") == "user"
            and conversation_message_text(message).strip() == normalized_prompt
        ):
            prompt_index = index
    return items[prompt_index + 1:] if prompt_index >= 0 else []


def parsed_tool_result(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    raw_text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(raw_text, str):
        return {}
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_records_from_conversation_data(
    data: dict[str, Any],
    prompt: str | None = None,
) -> list[dict[str, Any]]:
    items = current_turn_node_items(data, prompt)
    if not items:
        return []
    results_by_parent: dict[str, dict[str, Any]] = {}
    for node_id, node in items:
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        metadata = message.get("metadata")
        if (
            isinstance(author, dict)
            and author.get("role") == "tool"
            and author.get("name") == "api_tool.call_tool"
            and isinstance(metadata, dict)
        ):
            parent = node.get("parent")
            if isinstance(parent, str):
                results_by_parent[parent] = {
                    "node_id": node_id,
                    "tool": tool_name_from_call_path(
                        (metadata.get("invoked_resource") or {}).get("resource_uri")
                        if isinstance(metadata.get("invoked_resource"), dict)
                        else None
                    ),
                    "success": tool_result_succeeded(message),
                    "payload": parsed_tool_result(message),
                }

    records: list[dict[str, Any]] = []
    matched_result_nodes: set[str] = set()
    for node_id, node in items:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("recipient") == "api_tool.call_tool":
            content = message.get("content")
            raw_text = content.get("text") if isinstance(content, dict) else None
            try:
                call = json.loads(raw_text) if isinstance(raw_text, str) else {}
            except json.JSONDecodeError:
                call = {}
            tool = tool_name_from_call_path(call.get("path"))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            result = results_by_parent.get(node_id, {})
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            result_node_id = result.get("node_id")
            if isinstance(result_node_id, str):
                matched_result_nodes.add(result_node_id)
            records.append(
                {
                    "tool": tool,
                    "success": result.get("success") is True,
                    "path": args.get("path"),
                    "mode": args.get("mode"),
                    "workspace_id": args.get("workspaceId"),
                    "result_workspace_id": payload.get("workspaceId"),
                    "result_root": payload.get("root"),
                    "result_only": False,
                }
            )
            continue
        author = message.get("author")
        metadata = message.get("metadata")
        if (
            node_id in matched_result_nodes
            or not isinstance(author, dict)
            or author.get("role") != "tool"
            or author.get("name") != "api_tool.call_tool"
            or not isinstance(metadata, dict)
        ):
            continue
        resource = metadata.get("invoked_resource")
        tool = tool_name_from_call_path(
            resource.get("resource_uri") if isinstance(resource, dict) else None
        )
        if tool == "unknown":
            continue
        records.append(
            {
                "tool": tool,
                "success": tool_result_succeeded(message),
                "path": None,
                "mode": None,
                "workspace_id": None,
                "result_workspace_id": None,
                "result_root": None,
                "result_only": True,
            }
        )
    return records


def successful_workspace_id(records: list[dict[str, Any]]) -> str:
    for record in records:
        if record.get("tool") == "open_workspace" and record.get("success"):
            value = record.get("result_workspace_id")
            return value if isinstance(value, str) else ""
    return ""


def records_for_workspace(records: list[dict[str, Any]], workspace_id: str) -> list[dict[str, Any]]:
    if not workspace_id:
        return []
    selected: list[dict[str, Any]] = []
    for record in records:
        if record.get("tool") == "open_workspace":
            if record.get("result_workspace_id") == workspace_id:
                selected.append(record)
        elif record.get("workspace_id") == workspace_id:
            selected.append(record)
    return selected


def final_text_from_conversation_data(data: dict[str, Any], prompt: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return (
        advisor.latest_finished_assistant_text_for_prompt_data(data, prompt)
        or advisor.latest_finished_assistant_text(data)
    ).strip()


def remote_tool_records(
    state_path: Path,
    timeout: int,
    prompt: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    data, error = remote_conversation_data(state_path, timeout)
    return tool_records_from_conversation_data(data, prompt), error


def build_agent_prompt(
    *,
    task: str,
    role: str,
    workspace: Path,
    marker: str,
    allow_shell: bool,
) -> str:
    shell_rule = (
        "You may use shell only for read-only inspection or tests. Do not create, modify, move, or delete files."
        if allow_shell
        else "Do not call bash, exec_command, write_stdin, or any shell tool."
    )
    return f"""You are the {role} in a bounded repo-aware advisor run for Codex.

Use the connected custom DevSpace MCP app. Open exactly one workspace at this path:

{workspace}

This is a review-only run:
- Inspect only what is necessary to answer the task.
- Read applicable AGENTS.md instructions before files in their scope.
- After open_workspace succeeds, make at least one explicit read, grep, glob, or ls call for repository evidence; do not rely only on the open_workspace response.
- Do not write, edit, apply patches, show changes, install packages, start services, or alter git state.
- {shell_rule}
- Never inspect or print .env files, HAR files, cookies, tokens, private keys, wallet material, browser profiles, .codex-advisor, or unrelated private files.
- Cite inspected repository paths and line numbers when the tool provides them.
- Separate verified repository observations from recommendations and uncertainty.
- Codex remains the implementer and must verify your claims locally.

Task:
{task}

Finish with this exact marker on its own line:
{marker}
"""


def conversation_state_path(project: Path, run_dir: Path, conversation_key: str | None) -> Path:
    if not conversation_key:
        return run_dir / "conversation.json"
    key = safety.safe_key_slug(conversation_key, default="agent")
    root = project / ".codex-advisor" / "agent-conversations"
    safety.ensure_private_dir(root)
    return root / f"{key}.conversation.json"


def advisor_command(args: argparse.Namespace, response_path: Path) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("advisor.py")),
        "--provider",
        args.provider,
        "--timeout",
        str(args.timeout),
        "--save",
        str(response_path),
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.thinking_effort:
        command.extend(["--thinking-effort", args.thinking_effort])
    if not args.live_activity:
        command.append("--no-live-activity")
    return command


def validate_result(
    *,
    returncode: int,
    output: str,
    marker: str,
    evidence: ToolEvidence,
    min_inspection_calls: int,
    require_tool_activity: bool,
    corroborating_evidence: ToolEvidence | None = None,
) -> list[str]:
    errors: list[str] = []
    if returncode != 0:
        errors.append(f"advisor.py exited with status {returncode}")
    if not output.strip():
        errors.append("advisor.py returned no final response text")
    if marker not in output:
        errors.append("the final response did not include the run completion marker")
    if evidence.disallowed:
        errors.append("disallowed DevSpace tools were observed: " + ", ".join(evidence.disallowed))
    if require_tool_activity:
        if evidence.attempted_open_workspace_count != 1:
            errors.append(
                "expected exactly one DevSpace open_workspace attempt in the current turn "
                f"({evidence.attempted_open_workspace_count} observed)"
            )
        if evidence.open_workspace_count != 1:
            errors.append(
                "expected exactly one successful DevSpace open_workspace call in the current turn "
                f"({evidence.open_workspace_count} observed)"
            )
        if evidence.wrong_workspace_open_count:
            errors.append("the current turn did not open the exact expected review workspace")
        if evidence.inspection_before_open_count:
            errors.append("repository inspection occurred before the expected workspace was opened")
        if evidence.workspace_id_mismatch_count:
            errors.append("repository inspection used an unexpected DevSpace workspace id")
        if evidence.sensitive_path_attempt_count:
            errors.append("the current turn attempted to inspect a denied or escaping path")
        if evidence.inspection_count < min_inspection_calls:
            errors.append(
                "insufficient successful repository inspection calls "
                f"({evidence.inspection_count} observed, {min_inspection_calls} required)"
            )
        if corroborating_evidence is None:
            errors.append("private DevSpace workspace-id corroboration was unavailable")
        else:
            if corroborating_evidence.open_workspace_count != 1:
                errors.append(
                    "expected one matching open_workspace record in the private DevSpace log "
                    f"({corroborating_evidence.open_workspace_count} observed)"
                )
            if corroborating_evidence.disallowed:
                errors.append(
                    "disallowed tools were attributed to the opened DevSpace workspace: "
                    + ", ".join(corroborating_evidence.disallowed)
                )
            if corroborating_evidence.wrong_workspace_open_count:
                errors.append("the private DevSpace log did not match the expected review workspace")
            if corroborating_evidence.inspection_before_open_count:
                errors.append("the private DevSpace log recorded inspection before workspace open")
            if corroborating_evidence.workspace_id_mismatch_count:
                errors.append("the private DevSpace log recorded an unexpected workspace id")
            if corroborating_evidence.sensitive_path_attempt_count:
                errors.append("the opened DevSpace workspace attempted a denied or escaping path")
            if corroborating_evidence.inspection_count < min_inspection_calls:
                errors.append(
                    "insufficient workspace-id-attributed DevSpace inspection calls "
                    f"({corroborating_evidence.inspection_count} observed, {min_inspection_calls} required)"
                )
            remote_counts = Counter(
                tool for tool in evidence.successful if tool in INSPECTION_TOOLS
            )
            local_counts = Counter(
                tool for tool in corroborating_evidence.successful if tool in INSPECTION_TOOLS
            )
            missing = [
                f"{tool}:{count - local_counts[tool]}"
                for tool, count in remote_counts.items()
                if local_counts[tool] < count
            ]
            if missing:
                errors.append(
                    "exact-conversation inspection results lacked matching private DevSpace records: "
                    + ", ".join(missing)
                )
    return errors


def strip_completion_marker(output: str, marker: str) -> str:
    return "\n".join(
        line for line in output.splitlines() if line.strip() != marker
    ).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Repo-aware review task. Reads stdin when omitted.")
    parser.add_argument("--role", default="reviewer", help="Bounded advisor role name.")
    parser.add_argument("--project-dir", type=Path, help="Original project directory.")
    parser.add_argument("--provider", choices=["openai", "openai-compatible"], default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"))
    parser.add_argument("--base-url", default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL"))
    parser.add_argument(
        "--thinking-effort",
        default=(
            os.environ.get("ADVISOR_THINKING_EFFORT")
            or os.environ.get("ADVISOR_CHATGPT_THINKING_EFFORT")
            or os.environ.get("ADVISOR_INTELLIGENCE")
        ),
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_AGENT_TIMEOUT", str(DEFAULT_TIMEOUT))))
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=float(os.environ.get("ADVISOR_QUEUE_TIMEOUT", str(DEFAULT_QUEUE_TIMEOUT))),
        help="Maximum seconds to wait for the shared advisor worker/conversation locks.",
    )
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1600")))
    parser.add_argument("--conversation-key", help="Reuse one saved repo-aware advisor conversation.")
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Deprecated safety diagnostic; normal repo-aware advisor runs do not expose shell tools.",
    )
    parser.add_argument("--min-inspection-calls", type=int, default=1)
    parser.add_argument("--no-require-tool-activity", action="store_true", help="Diagnostic only: do not fail when no MCP tool activity is observed.")
    parser.add_argument("--save", help="Optional extra path for the verified final response.")
    parser.add_argument("--json", action="store_true", help="Print only run metadata JSON.")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("ADVISOR_AGENT_DRY_RUN", "").lower() in {"1", "true", "yes", "on"})
    activity = parser.add_mutually_exclusive_group()
    activity.add_argument("--live-activity", dest="live_activity", action="store_true")
    activity.add_argument("--no-live-activity", dest="live_activity", action="store_false")
    parser.set_defaults(live_activity=True)
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    project = resolve_project(args.project_dir)
    task = safety.redact_sensitive_text(
        safety.sanitize_text(args.prompt if args.prompt is not None else sys.stdin.read())
    ).strip()
    if not task:
        print("Provide --prompt or pipe task text on stdin.", file=sys.stderr)
        return 2
    if args.min_inspection_calls < 0:
        print("--min-inspection-calls cannot be negative.", file=sys.stderr)
        return 2
    if args.timeout < 1:
        print("--timeout must be at least 1 second.", file=sys.stderr)
        return 2
    if args.queue_timeout < 0:
        print("--queue-timeout cannot be negative.", file=sys.stderr)
        return 2
    if args.allow_shell:
        print(
            "--allow-shell is disabled: the repo-aware advisor connector is mechanically read-only.",
            file=sys.stderr,
        )
        return 2

    run_dir = private_run_dir(project, args.role)
    response_path = run_dir / "response.md"
    metadata_path = run_dir / "meta.json"
    marker = f"ADVISOR-AGENT-{uuid.uuid4().hex.upper()}-COMPLETE"
    started = time.monotonic()

    if args.dry_run:
        payload = {
            "schema_version": "1.0",
            "created_utc": utc_now(),
            "status": "dry-run",
            "project_dir": str(project),
            "role": args.role,
            "task": task,
            "run_dir": str(run_dir),
        }
        safety.atomic_write_json(metadata_path, payload)
        print(json.dumps(payload, indent=2) if args.json else f"Advisor agent dry run saved: {metadata_path}")
        return 0

    errors: list[str] = []
    workspace = project
    agent_status: dict[str, Any] = {}
    state: dict[str, Any] = {}
    log_path: Path | None = None
    before_count = 0
    state_path: Path | None = None
    try:
        state = connector_status(project)
        if not state.get("connector_ready"):
            raise RuntimeError("The registered DevSpace connector is not ready for this project.")
        workspace, agent_status = refresh_agent_workspace(project)
        validate_workspace_for_connector(workspace, state)
        log_path = activity_monitor.discover_log_path(project)
        if log_path is None:
            raise RuntimeError("Could not validate the private DevSpace tool log for this project.")
        before_count = len(read_tool_records(log_path))
    except RuntimeError as exc:
        errors.append(str(exc))

    output = ""
    response_source = "advisor-transport"
    returncode = 1
    evidence = ToolEvidence(
        total=0,
        sequence=[],
        successful=[],
        failed=[],
        disallowed=[],
        attempted_open_workspace_count=0,
        open_workspace_count=0,
        inspection_count=0,
        wrong_workspace_open_count=0,
        inspection_before_open_count=0,
        workspace_id_mismatch_count=0,
        sensitive_path_attempt_count=0,
    )
    prompt = build_agent_prompt(
        task=task,
        role=args.role,
        workspace=workspace,
        marker=marker,
        allow_shell=args.allow_shell,
    )
    if not errors:
        state_path = conversation_state_path(project, run_dir, args.conversation_key)
        env = os.environ.copy()
        env["ADVISOR_PROJECT_DIR"] = str(project)
        env["ADVISOR_PROVIDER"] = args.provider
        env["ADVISOR_BASE_URL"] = args.base_url
        env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
        env["ADVISOR_QUEUE_TIMEOUT"] = str(args.queue_timeout)
        env["ADVISOR_STATE_PATH"] = str(state_path)
        env["ADVISOR_RESPONSE_PATH"] = str(response_path)
        env["ADVISOR_AUTO_CREATE_PROJECT"] = "false"
        if args.model:
            env["ADVISOR_MODEL"] = args.model
        if args.thinking_effort:
            env["ADVISOR_THINKING_EFFORT"] = args.thinking_effort
        try:
            completed = subprocess.run(
                advisor_command(args, response_path),
                cwd=project,
                env=env,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=None,
                timeout=args.queue_timeout + args.timeout + 30,
            )
            returncode = completed.returncode
            output = completed.stdout.strip()
        except subprocess.TimeoutExpired:
            errors.append(f"repo-aware advisor exceeded the {args.timeout}-second timeout")
        except OSError as exc:
            errors.append(f"could not launch advisor.py: {exc}")

        local_window_records = read_tool_records(log_path)[before_count:] if log_path is not None else []
        remote_evidence_error = ""
        remote_records: list[dict[str, Any]] = []
        if state_path is not None:
            remote_data, remote_evidence_error = remote_conversation_data(state_path, args.timeout)
            remote_records = tool_records_from_conversation_data(remote_data, prompt)
            remote_final = final_text_from_conversation_data(remote_data, prompt)
            if remote_final and marker in remote_final:
                output = remote_final
                response_source = "chatgpt-conversation-final"
        evidence = summarize_tool_evidence(
            remote_records,
            allow_shell=args.allow_shell,
            expected_workspace=workspace,
        )
        workspace_id = successful_workspace_id(remote_records)
        local_records = records_for_workspace(local_window_records, workspace_id)
        local_evidence = summarize_tool_evidence(
            local_records,
            allow_shell=args.allow_shell,
            expected_workspace=workspace,
        )
        if remote_evidence_error and not args.no_require_tool_activity:
            errors.append(remote_evidence_error)
        errors.extend(
            validate_result(
                returncode=returncode,
                output=output,
                marker=marker,
                evidence=evidence,
                min_inspection_calls=args.min_inspection_calls,
                require_tool_activity=not args.no_require_tool_activity,
                corroborating_evidence=local_evidence,
            )
        )
        if marker in output:
            output = strip_completion_marker(output, marker)
    else:
        local_records = []
        local_window_records = []
        local_evidence = summarize_tool_evidence([], allow_shell=args.allow_shell)
        remote_evidence_error = ""

    if output:
        safety.atomic_write_text(response_path, output.rstrip() + "\n")
    if args.save and output and not errors:
        safety.atomic_write_text(Path(args.save), output.rstrip() + "\n")

    payload = {
        "schema_version": "1.0",
        "created_utc": utc_now(),
        "status": "ok" if not errors else "failed",
        "project_dir": str(project),
        "workspace_dir": str(workspace),
        "role": args.role,
        "task": task,
        "provider": args.provider,
        "model": args.model or DEFAULT_MODEL,
        "thinking_effort": args.thinking_effort or "max",
        "request_timeout_seconds": args.timeout,
        "queue_timeout_seconds": args.queue_timeout,
        "allow_shell": args.allow_shell,
        "response_source": response_source,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "tool_evidence": evidence.to_dict(),
        "tool_evidence_scope": "chatgpt-conversation",
        "project_log_observability": summarize_tool_evidence(
            local_window_records,
            allow_shell=args.allow_shell,
        ).to_dict(),
        "project_log_scope": "shared-project-window",
        "project_log_attributed_evidence": local_evidence.to_dict(),
        "project_log_attribution_scope": "matching-open-workspace-id",
        "remote_evidence_error": remote_evidence_error,
        "errors": errors,
        "response_path": str(response_path),
        "run_dir": str(run_dir),
        "agent_mode": agent_status,
        "connector_schema_version": state.get("schema_version"),
    }
    safety.atomic_write_json(metadata_path, payload)

    if args.json:
        print(json.dumps(payload, indent=2))
    elif errors:
        print("Repo-aware advisor failed closed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(f"Agent run metadata saved: {metadata_path}", file=sys.stderr)
        if output:
            print(f"Unverified response saved: {response_path}", file=sys.stderr)
    else:
        print(output)
        print(f"\nAdvisor agent response saved: {response_path}", file=sys.stderr)
        print(f"Advisor agent metadata saved: {metadata_path}", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
