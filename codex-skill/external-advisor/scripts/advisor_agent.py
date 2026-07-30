#!/usr/bin/env python3
"""Run one bounded repo-aware ChatGPT advisor through the registered DevSpace MCP."""

from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import activity_monitor
import advisor_agent_connect
import advisor_concurrency as concurrency
import advisor_safety as safety
import agent_mode


INSPECTION_TOOLS = {"read", "grep", "glob", "ls"}
SHELL_TOOLS = {"bash", "exec_command", "write_stdin"}
MUTATION_TOOLS = {"write", "edit", "apply_patch", "show_changes"}
SAFE_TOOL_NAMES = {"open_workspace", *INSPECTION_TOOLS, *SHELL_TOOLS, *MUTATION_TOOLS}
DEFAULT_MODEL = "gpt-5-6-thinking"
DEFAULT_TIMEOUT = 0
DEFAULT_QUEUE_TIMEOUT = 0.0
MAX_PRIVATE_TOOL_LOG_BYTES = 16 * 1024 * 1024


@dataclass
class ToolEvidence:
    total: int
    sequence: list[str]
    successful: list[str]
    result_only_successful: list[str]
    failed: list[str]
    disallowed: list[str]
    attempted_open_workspace_count: int
    open_workspace_count: int
    failed_open_workspace_count: int
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
            "result_only_successful": self.result_only_successful,
            "failed": self.failed,
            "disallowed": self.disallowed,
            "attempted_open_workspace_count": self.attempted_open_workspace_count,
            "open_workspace_count": self.open_workspace_count,
            "failed_open_workspace_count": self.failed_open_workspace_count,
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


def combined_subprocess_timeout(request_timeout: float, queue_timeout: float, cushion: float) -> float | None:
    if request_timeout <= 0 or queue_timeout <= 0:
        return None
    return request_timeout + queue_timeout + cushion


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


def validated_run_dir(project: Path, raw: Path, *, create: bool) -> Path:
    root = (project / ".codex-advisor").resolve()
    path = raw.expanduser().resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Advisor agent run directories must stay under the project's .codex-advisor directory.") from exc
    if not relative.parts:
        raise RuntimeError("The project .codex-advisor root cannot be used as an agent run directory.")
    if create:
        safety.ensure_private_dir(path)
    elif not path.is_dir():
        raise RuntimeError("The requested advisor agent run directory does not exist.")
    return path


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def journal_proves_submission(journal: dict[str, Any]) -> bool:
    return str(journal.get("phase") or "") in {
        "submission-started",
        "submission-outcome-unknown",
        "response-received",
        "conversation-persisted",
        "completed",
    }


def validate_recovery_marker(value: str | None) -> str:
    marker = value or f"ADVISOR-AGENT-{uuid.uuid4().hex.upper()}-COMPLETE"
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,160}", marker):
        raise RuntimeError("The advisor recovery marker must be 16-160 ASCII letters, digits, underscores, or hyphens.")
    return marker


def connector_status(project: Path) -> dict[str, Any]:
    root = advisor_agent_connect.runtime_root()
    return advisor_agent_connect.connector_runtime_status(project, root=root)


def mark_chatgpt_attachment_verified(project: Path, expected_state: dict[str, Any]) -> bool:
    """Mark only the unchanged live connector that produced verified MCP evidence."""
    root = advisor_agent_connect.runtime_root()
    paths = advisor_agent_connect.state_paths(project, root)
    stable_fields = (
        "started_utc",
        "devspace_pid",
        "devspace_process_identity",
        "tunnel_pid",
        "tunnel_process_identity",
        "mcp_url",
        "agent_workspace",
        "allowed_root",
        "readonly_exact_root_file",
    )
    with concurrency.InterProcessLock(paths["lock"], timeout=30.0):
        current = advisor_agent_connect.read_state(paths["state"])
        if not current or any(current.get(key) != expected_state.get(key) for key in stable_fields):
            return False
        skip_public_probe = os.environ.get("ADVISOR_AGENT_SKIP_PUBLIC_PROBE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        runtime = advisor_agent_connect.connector_runtime_status(
            project,
            root=root,
            skip_public_probe=skip_public_probe,
        )
        if not runtime.get("connector_ready"):
            return False
        current.update(
            {
                "chatgpt_attachment_verified": True,
                "chatgpt_attachment_verified_utc": utc_now(),
                "agent_mode_ready": True,
            }
        )
        safety.atomic_write_json(paths["state"], current)
    return True


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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(log_path, flags)
    except OSError:
        return records
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return records
        offset = max(0, metadata.st_size - MAX_PRIVATE_TOOL_LOG_BYTES)
        os.lseek(descriptor, offset, os.SEEK_SET)
        remaining = metadata.st_size - offset
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if offset:
        separator = data.find(b"\n")
        data = data[separator + 1 :] if separator >= 0 else b""
    lines = data.decode("utf-8", errors="replace").splitlines()
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("event") != "tool_call":
            continue
        tool = record.get("tool")
        success = record.get("success")
        if not isinstance(tool, str) or not tool.strip() or not isinstance(success, bool):
            continue
        normalized_tool = tool if tool in SAFE_TOOL_NAMES else "unknown"
        workspace_id = record.get("workspaceId")
        path = record.get("path")
        item = {
            "tool": normalized_tool,
            "success": success,
            "workspace_id": workspace_id if isinstance(workspace_id, str) else "",
            "path": path if isinstance(path, str) else None,
            "selectors": inspection_path_selectors(normalized_tool, record),
            "source": "project-log",
        }
        if normalized_tool == "open_workspace":
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
    result_only_successful = [
        str(record["tool"])
        for record in records
        if record["success"] and record.get("result_only")
    ]
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
        if tool == "open_workspace":
            raw_path = record.get("path")
            result_root = record.get("result_root")
            mode = record.get("mode")
            if expected is not None:
                opened_path: Path | None = None
                returned_root: Path | None = None
                if not record.get("result_only"):
                    try:
                        opened_path = Path(str(raw_path)).expanduser().resolve()
                    except (OSError, TypeError, ValueError):
                        opened_path = None
                if record.get("success"):
                    try:
                        returned_root = Path(str(result_root)).expanduser().resolve()
                    except (OSError, TypeError, ValueError):
                        returned_root = None
                request_wrong = bool(
                    not record.get("result_only")
                    and (opened_path != expected or mode not in (None, "", "checkout"))
                )
                result_wrong = bool(record.get("success") and returned_root != expected)
                if request_wrong or result_wrong:
                    wrong_workspace_open_count += 1
                    if raw_path not in (None, "") and tool_path_is_sensitive(raw_path):
                        sensitive_path_attempt_count += 1
            if not record.get("success"):
                continue
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
        selectors = record.get("selectors")
        if not isinstance(selectors, list):
            selectors = inspection_path_selectors(tool, record)
        if any(tool_path_is_sensitive(value) for value in selectors):
            sensitive_path_attempt_count += 1
    return ToolEvidence(
        total=len(records),
        sequence=sequence,
        successful=successful,
        result_only_successful=result_only_successful,
        failed=failed,
        disallowed=disallowed,
        attempted_open_workspace_count=attempted_open_workspace_count,
        open_workspace_count=successful.count("open_workspace"),
        failed_open_workspace_count=sum(
            1
            for record in records
            if record.get("tool") == "open_workspace" and not record.get("success")
        ),
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

DENIED_GLOB_CANDIDATES = {
    ".codex-advisor",
    ".env",
    ".env.local",
    ".git",
    ".ssh",
    "auth_openaichat.json",
    "chat.har",
    "har_and_cookies",
    "secret.key",
    "secret.pem",
    "wallets",
}


def bounded_brace_expansions(pattern: str, limit: int = 32) -> list[str]:
    expanded = [pattern]
    while len(expanded) <= limit:
        changed = False
        next_values: list[str] = []
        for value in expanded:
            match = re.search(r"\{([^{}]+)\}", value)
            if match is None:
                next_values.append(value)
                continue
            choices = match.group(1).split(",")
            if len(next_values) + len(choices) > limit:
                return [pattern]
            changed = True
            next_values.extend(
                value[:match.start()] + choice + value[match.end():]
                for choice in choices
            )
        expanded = next_values
        if not changed:
            return expanded
    return [pattern]


def glob_selector_can_match_sensitive_name(pattern: str) -> bool:
    for expanded in bounded_brace_expansions(pattern.lower()):
        for segment in (item for item in expanded.split("/") if item not in ("", ".")):
            if segment in {"*", "**", "?"}:
                continue
            if any(fnmatch.fnmatchcase(candidate, segment) for candidate in DENIED_GLOB_CANDIDATES):
                return True
    return False


def tool_path_is_sensitive(raw_path: Any) -> bool:
    if raw_path in (None, ""):
        return False
    if not isinstance(raw_path, str):
        return True
    normalized = raw_path.replace("\\", "/").strip()
    if (
        not normalized
        or normalized.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", normalized) is not None
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", normalized) is not None
    ):
        return True
    parts = [part.lower() for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts or any(part in DENIED_INSPECTION_PATH_PARTS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name == ".env" or name.startswith(".env."):
        return True
    if name.startswith("auth_") and name.endswith(".json"):
        return True
    if any(name.endswith(suffix) for suffix in safety.SENSITIVE_SUFFIXES):
        return True
    return glob_selector_can_match_sensitive_name(normalized)


def inspection_path_selectors(tool: str, values: dict[str, Any]) -> list[Any]:
    """Return every argument that can select a path for a read-only tool."""
    keys = ["path"]
    if tool == "glob":
        keys.append("pattern")
    elif tool == "grep":
        keys.append("include")
    return [values.get(key) for key in keys if values.get(key) not in (None, "")]


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
        data = advisor.get_remote_json_with_backoff(
            f"https://chatgpt.com/backend-api/conversation/{conversation_id}",
            auth["headers"],
            60 if timeout <= 0 else min(timeout, 60),
            operation="repo-aware tool evidence fetch",
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
                    "selectors": inspection_path_selectors(tool, args),
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
        records.append(
            {
                "tool": tool,
                "success": tool_result_succeeded(message),
                "path": None,
                "selectors": [],
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

    # Recovery must stay anchored to the checkpointed user turn. A later turn
    # in the same web conversation is not evidence that this turn completed.
    return advisor.latest_finished_assistant_text_for_prompt_data(data, prompt).strip()


def conversation_contains_exact_prompt(data: dict[str, Any], prompt: str) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    normalized = prompt.strip()
    for item in advisor.transcript_from_conversation(data):
        if item.get("role") == "user" and str(item.get("content") or "").strip() == normalized:
            return True
    return False


def listed_project_conversations(project_id: str, timeout: int) -> tuple[list[dict[str, Any]], str]:
    """Read recent project conversations without submitting a ChatGPT turn."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    auth = advisor.load_chatgpt_auth()
    if not auth:
        return [], "ChatGPT HAR/auth is unavailable for interrupted-run recovery"
    items: list[dict[str, Any]] = []
    cursor = ""
    try:
        max_pages = max(1, min(20, int(os.environ.get("ADVISOR_AGENT_RECOVERY_MAX_PAGES", "5"))))
    except ValueError:
        return [], "ADVISOR_AGENT_RECOVERY_MAX_PAGES must be an integer between 1 and 20"
    for _page in range(max_pages):
        query: dict[str, str | int] = {"limit": 50, "owned_only": "true"}
        if cursor:
            query["cursor"] = cursor
        url = (
            f"https://chatgpt.com/backend-api/gizmos/{project_id}/conversations?"
            + urlencode(query)
        )
        try:
            payload = advisor.get_remote_json_with_backoff(
                url,
                auth["headers"],
                60 if timeout <= 0 else min(timeout, 60),
                operation="interrupted agent conversation discovery",
            )
        except RuntimeError as exc:
            return [], "could not list project conversations for recovery: " + safety.redact_sensitive_text(str(exc))
        page_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(page_items, list):
            return [], "ChatGPT project conversation discovery returned no item list"
        items.extend(item for item in page_items if isinstance(item, dict))
        next_cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return items, ""


def fetch_conversation_by_id(conversation_id: str, timeout: int) -> tuple[dict[str, Any], str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    auth = advisor.load_chatgpt_auth()
    if not auth:
        return {}, "ChatGPT HAR/auth is unavailable for interrupted-run recovery"
    try:
        data = advisor.get_remote_json_with_backoff(
            f"https://chatgpt.com/backend-api/conversation/{conversation_id}",
            auth["headers"],
            60 if timeout <= 0 else min(timeout, 60),
            operation="interrupted agent conversation fetch",
        )
    except RuntimeError as exc:
        return {}, "could not fetch the interrupted ChatGPT conversation: " + safety.redact_sensitive_text(str(exc))
    return data, ""


def discover_exact_remote_conversation(
    project_id: str,
    prompt: str,
    timeout: int,
) -> tuple[dict[str, Any], str, str]:
    items, error = listed_project_conversations(project_id, timeout)
    if error:
        return {}, "", error
    matches: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        conversation_id = item.get("id") or item.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        if conversation_contains_exact_prompt(item, prompt):
            matches.append((conversation_id, item))
    unique = {conversation_id: data for conversation_id, data in matches}
    if len(unique) > 1:
        return {}, "", "multiple ChatGPT conversations matched the unique interrupted-run prompt; refusing ambiguous recovery"
    if not unique:
        return {}, "", "the submitted turn is not yet discoverable in the bound ChatGPT Project"
    conversation_id, listed_data = next(iter(unique.items()))
    data, fetch_error = fetch_conversation_by_id(conversation_id, timeout)
    if fetch_error:
        # The project listing already carried a conversation graph. It is safe
        # to report the read failure as pending, but never to submit again.
        return listed_data, conversation_id, fetch_error
    if not conversation_contains_exact_prompt(data, prompt):
        return {}, "", "the fetched ChatGPT conversation did not retain the exact interrupted-run prompt"
    return data, conversation_id, ""


def persist_recovered_conversation(
    *,
    state_path: Path,
    project_id: str,
    conversation_id: str,
    data: dict[str, Any],
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    transcript = advisor.transcript_from_conversation(data)
    message_id = advisor.latest_finished_assistant_message_id(data) or advisor.latest_message_id(data, transcript)
    conversation: dict[str, Any] = {"conversation_id": conversation_id}
    if isinstance(message_id, str) and message_id:
        conversation["message_id"] = message_id
        conversation["parent_message_id"] = message_id
    auth = advisor.load_chatgpt_auth()
    if auth and auth.get("user_id"):
        conversation["user_id"] = auth["user_id"]
    advisor.write_transcript(state_path, data, transcript)
    safety.atomic_write_json(
        state_path,
        {
            "conversation": conversation,
            "chatgpt_project_id": project_id,
        },
    )


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
        corroborated_single_open = bool(
            corroborating_evidence is not None
            and corroborating_evidence.attempted_open_workspace_count == 1
            and corroborating_evidence.open_workspace_count == 1
        )
        graph_only_failed_open = bool(
            evidence.attempted_open_workspace_count == 2
            and evidence.open_workspace_count == 1
            and evidence.failed_open_workspace_count == 1
            and corroborated_single_open
        )
        if evidence.attempted_open_workspace_count != 1 and not graph_only_failed_open:
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
            if corroborating_evidence.attempted_open_workspace_count != 1:
                errors.append(
                    "expected one matching open_workspace attempt in the private DevSpace log "
                    f"({corroborating_evidence.attempted_open_workspace_count} observed)"
                )
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
                tool for tool in evidence.result_only_successful if tool in INSPECTION_TOOLS
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


def emit_resume_payload(payload: dict[str, Any], *, as_json: bool) -> int:
    status = str(payload.get("status") or "failed")
    if as_json:
        print(json.dumps(payload, indent=2))
    elif status == "ok":
        response_path = Path(str(payload.get("response_path") or ""))
        if response_path.is_file():
            print(response_path.read_text(encoding="utf-8", errors="replace").strip())
        print(f"\nAdvisor agent run recovered: {payload.get('run_dir', '')}", file=sys.stderr)
    else:
        detail = str(payload.get("resume_detail") or "interrupted run is not recoverable yet")
        print(f"Advisor agent resume status: {status}: {detail}", file=sys.stderr)
    if status == "ok":
        return 0
    if status in {"not-submitted", "remote-pending"}:
        return 3
    return 1


def resume_agent_run(args: argparse.Namespace, request: dict[str, Any]) -> int:
    raw_project = args.project_dir or Path(str(request.get("project_dir") or ""))
    project = resolve_project(raw_project)
    run_dir = validated_run_dir(project, args.resume_run_dir, create=False)
    request_path = run_dir / "request.json"
    request = read_json_object(request_path)
    if not request:
        raise RuntimeError("The interrupted advisor run has no readable request.json checkpoint.")
    if Path(str(request.get("project_dir") or "")).expanduser().resolve() != project:
        raise RuntimeError("The interrupted advisor request belongs to a different project directory.")

    metadata_path = run_dir / "meta.json"
    existing = read_json_object(metadata_path)
    response_path = run_dir / "response.md"
    if existing.get("status") == "ok" and response_path.is_file():
        return emit_resume_payload(existing, as_json=args.json)

    prompt = str(request.get("prompt") or "")
    marker = str(request.get("marker") or "")
    role = str(request.get("role") or "reviewer")
    task = str(request.get("task") or "")
    workspace = Path(str(request.get("workspace_dir") or "")).expanduser().resolve()
    if not prompt or not marker or marker not in prompt:
        raise RuntimeError("The interrupted advisor request lacks its exact prompt recovery marker.")
    state_path = Path(str(request.get("state_path") or run_dir / "conversation.json")).expanduser().resolve()
    try:
        state_path.relative_to((project / ".codex-advisor").resolve())
    except ValueError as exc:
        raise RuntimeError("The interrupted advisor state path escaped the project's .codex-advisor directory.") from exc
    journal_path = Path(str(request.get("journal_path") or run_dir / "turn-journal.json")).expanduser().resolve()
    try:
        journal_path.relative_to((project / ".codex-advisor").resolve())
    except ValueError as exc:
        raise RuntimeError("The interrupted advisor journal path escaped the project's .codex-advisor directory.") from exc
    journal = read_json_object(journal_path)
    saved_state = read_json_object(state_path)
    saved_conversation = saved_state.get("conversation") if isinstance(saved_state.get("conversation"), dict) else {}
    conversation_id = saved_conversation.get("conversation_id") if isinstance(saved_conversation, dict) else None

    base_payload: dict[str, Any] = {
        "schema_version": "1.1",
        "created_utc": utc_now(),
        "project_dir": str(project),
        "workspace_dir": str(workspace),
        "role": role,
        "task": task,
        "provider": str(request.get("provider") or "openai-compatible"),
        "model": str(request.get("model") or DEFAULT_MODEL),
        "thinking_effort": str(request.get("thinking_effort") or "max"),
        "request_timeout_seconds": int(request.get("request_timeout_seconds") or 0),
        "queue_timeout_seconds": float(request.get("queue_timeout_seconds") or 0),
        "allow_shell": bool(request.get("allow_shell")),
        "response_source": "interrupted-run-remote-recovery",
        "response_path": str(response_path),
        "run_dir": str(run_dir),
        "resumed": True,
        "journal_phase": str(journal.get("phase") or ""),
    }

    if not conversation_id and not journal_proves_submission(journal):
        payload = {
            **base_payload,
            "status": "not-submitted",
            "safe_to_submit": True,
            "resume_detail": "the local journal proves no ChatGPT turn submission began",
            "errors": [],
        }
        safety.atomic_write_json(metadata_path, payload)
        return emit_resume_payload(payload, as_json=args.json)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    project_id = str(request.get("chatgpt_project_id") or saved_state.get("chatgpt_project_id") or "")
    if not project_id:
        project_id = str(advisor.chatgpt_project_id(allow_create=False) or "")
    if not project_id:
        payload = {
            **base_payload,
            "status": "remote-pending",
            "safe_to_submit": False,
            "resume_detail": "the interrupted run has no bound ChatGPT Project id",
            "errors": [],
        }
        safety.atomic_write_json(metadata_path, payload)
        return emit_resume_payload(payload, as_json=args.json)

    remote_error = ""
    if isinstance(conversation_id, str) and conversation_id:
        remote_data, remote_error = fetch_conversation_by_id(conversation_id, args.timeout)
    else:
        remote_data, conversation_id, remote_error = discover_exact_remote_conversation(
            project_id,
            prompt,
            args.timeout,
        )
    if remote_error or not remote_data or not conversation_id:
        payload = {
            **base_payload,
            "status": "remote-pending",
            "safe_to_submit": False,
            "resume_detail": remote_error or "the submitted ChatGPT turn is not yet available",
            "errors": [],
        }
        safety.atomic_write_json(metadata_path, payload)
        return emit_resume_payload(payload, as_json=args.json)
    if not conversation_contains_exact_prompt(remote_data, prompt):
        payload = {
            **base_payload,
            "status": "failed",
            "safe_to_submit": False,
            "resume_detail": "the recovered conversation did not contain the exact checkpointed prompt",
            "errors": ["exact interrupted-run prompt not found"],
        }
        safety.atomic_write_json(metadata_path, payload)
        return emit_resume_payload(payload, as_json=args.json)

    output = final_text_from_conversation_data(remote_data, prompt)
    if not output:
        payload = {
            **base_payload,
            "status": "remote-pending",
            "safe_to_submit": False,
            "resume_detail": "the exact ChatGPT turn exists but has not produced a finished final response",
            "errors": [],
        }
        safety.atomic_write_json(metadata_path, payload)
        return emit_resume_payload(payload, as_json=args.json)

    remote_records = tool_records_from_conversation_data(remote_data, prompt)
    evidence = summarize_tool_evidence(
        remote_records,
        allow_shell=bool(request.get("allow_shell")),
        expected_workspace=workspace,
    )
    log_path = Path(str(request.get("log_path") or "")).expanduser()
    all_local_records = read_tool_records(log_path) if log_path.is_file() else []
    workspace_id = successful_workspace_id(remote_records)
    local_records = records_for_workspace(all_local_records, workspace_id)
    local_evidence = summarize_tool_evidence(
        local_records,
        allow_shell=bool(request.get("allow_shell")),
        expected_workspace=workspace,
    )
    errors = validate_result(
        returncode=0,
        output=output,
        marker=marker,
        evidence=evidence,
        min_inspection_calls=int(request.get("min_inspection_calls") or 1),
        require_tool_activity=bool(request.get("require_tool_activity", True)),
        corroborating_evidence=local_evidence,
    )
    clean_output = strip_completion_marker(output, marker) if marker in output else output.strip()
    persist_recovered_conversation(
        state_path=state_path,
        project_id=project_id,
        conversation_id=conversation_id,
        data=remote_data,
    )
    if clean_output:
        safety.atomic_write_text(response_path, clean_output.rstrip() + "\n")
    payload = {
        **base_payload,
        "status": "ok" if not errors else "failed",
        "safe_to_submit": False,
        "resume_detail": "completed remote response recovered and verified" if not errors else "remote response failed repo-aware evidence validation",
        "tool_evidence": evidence.to_dict(),
        "tool_evidence_scope": "chatgpt-conversation",
        "project_log_attributed_evidence": local_evidence.to_dict(),
        "project_log_attribution_scope": "matching-open-workspace-id",
        "errors": errors,
    }
    safety.atomic_write_json(metadata_path, payload)
    return emit_resume_payload(payload, as_json=args.json)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Repo-aware review task. Reads stdin when omitted.")
    parser.add_argument("--role", default="reviewer", help="Bounded advisor role name.")
    parser.add_argument("--project-dir", type=Path, help="Original project directory.")
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Internal orchestration option: preassign a private run directory under .codex-advisor.",
    )
    parser.add_argument(
        "--resume-run-dir",
        type=Path,
        help="Recover one interrupted run using read-only ChatGPT requests; never submits a new turn.",
    )
    parser.add_argument(
        "--recovery-token",
        help="Internal orchestration option: unique marker embedded in the submitted prompt.",
    )
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
        help="Maximum seconds to wait for worker/conversation locks; 0 waits until available.",
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
    if args.run_dir is not None and args.resume_run_dir is not None:
        print("--run-dir and --resume-run-dir are mutually exclusive.", file=sys.stderr)
        return 2
    if args.resume_run_dir is not None:
        request = read_json_object(args.resume_run_dir.expanduser().resolve() / "request.json")
        try:
            return resume_agent_run(args, request)
        except RuntimeError as exc:
            print(f"Advisor agent resume failed: {exc}", file=sys.stderr)
            return 2
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
    if args.timeout < 0:
        print("--timeout cannot be negative; use 0 to wait until the remote turn finishes.", file=sys.stderr)
        return 2
    if args.queue_timeout < 0:
        print("--queue-timeout cannot be negative.", file=sys.stderr)
        return 2
    if args.provider != "openai-compatible" or not concurrency.local_http_url(args.base_url):
        print(
            "Repo-aware advisor mode requires a loopback OpenAI-compatible endpoint; "
            "refusing to send repository-derived prompts to a remote API.",
            file=sys.stderr,
        )
        return 2
    effective_timeout = concurrency.effective_agent_timeout(args.timeout)
    if effective_timeout != args.timeout:
        print(
            "Advisor ignored the legacy 900-second agent cutoff and will wait for the final "
            "ChatGPT turn. Set ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT=true only for a deliberate "
            "bounded diagnostic.",
            file=sys.stderr,
        )
        args.timeout = effective_timeout
    if args.allow_shell:
        print(
            "--allow-shell is disabled: the repo-aware advisor connector is mechanically read-only.",
            file=sys.stderr,
        )
        return 2

    try:
        run_dir = (
            validated_run_dir(project, args.run_dir, create=True)
            if args.run_dir is not None
            else private_run_dir(project, args.role)
        )
        marker = validate_recovery_marker(args.recovery_token)
    except RuntimeError as exc:
        print(f"Advisor agent setup failed: {exc}", file=sys.stderr)
        return 2
    response_path = run_dir / "response.md"
    metadata_path = run_dir / "meta.json"
    request_path = run_dir / "request.json"
    journal_path = run_dir / "turn-journal.json"
    existing_request = read_json_object(request_path)
    existing_journal = read_json_object(journal_path)
    if existing_request and journal_proves_submission(existing_journal):
        print(
            "This run directory may already have submitted a ChatGPT turn. "
            "Use --resume-run-dir so recovery remains GET-only.",
            file=sys.stderr,
        )
        return 2
    state_path = conversation_state_path(project, run_dir, args.conversation_key)
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
    try:
        state = connector_status(project)
        if not state.get("connector_ready"):
            raise RuntimeError("The registered DevSpace connector is not ready for this project.")
        workspace, agent_status = refresh_agent_workspace(project)
        validate_workspace_for_connector(workspace, state)
        sanitized = agent_status.get("sanitized_workspace")
        sanitized = sanitized if isinstance(sanitized, dict) else {}
        state = advisor_agent_connect.pin_connector_workspace(
            project,
            state,
            workspace,
            generation=str(sanitized.get("generation_id") or ""),
            fingerprint=str(sanitized.get("source_fingerprint") or ""),
        )
        if not state.get("connector_ready") or not state.get("readonly_exact_root_ready"):
            raise RuntimeError("The live connector did not retain the exact refreshed review snapshot pin.")
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
        result_only_successful=[],
        failed=[],
        disallowed=[],
        attempted_open_workspace_count=0,
        open_workspace_count=0,
        failed_open_workspace_count=0,
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    project_id = advisor.chatgpt_project_id(allow_create=False)
    request_payload = {
        "schema_version": "1.0",
        "created_utc": utc_now(),
        "status": "ready-to-submit" if not errors else "preflight-failed",
        "project_dir": str(project),
        "workspace_dir": str(workspace),
        "role": args.role,
        "task": task,
        "marker": marker,
        "prompt": prompt,
        "state_path": str(state_path),
        "journal_path": str(journal_path),
        "log_path": str(log_path) if log_path is not None else "",
        "log_start_record_count": before_count,
        "chatgpt_project_id": project_id or "",
        "provider": args.provider,
        "model": args.model or DEFAULT_MODEL,
        "thinking_effort": args.thinking_effort or "max",
        "request_timeout_seconds": args.timeout,
        "queue_timeout_seconds": args.queue_timeout,
        "allow_shell": args.allow_shell,
        "min_inspection_calls": args.min_inspection_calls,
        "require_tool_activity": not args.no_require_tool_activity,
    }
    safety.atomic_write_json(request_path, request_payload)
    if not errors:
        env = os.environ.copy()
        env["ADVISOR_PROJECT_DIR"] = str(project)
        env["ADVISOR_PROVIDER"] = args.provider
        env["ADVISOR_BASE_URL"] = args.base_url
        env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
        env["ADVISOR_QUEUE_TIMEOUT"] = str(args.queue_timeout)
        env["ADVISOR_STATE_PATH"] = str(state_path)
        env["ADVISOR_RESPONSE_PATH"] = str(response_path)
        env["ADVISOR_TURN_JOURNAL_PATH"] = str(journal_path)
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
                timeout=combined_subprocess_timeout(args.timeout, args.queue_timeout, 30),
            )
            returncode = completed.returncode
            output = completed.stdout.strip()
        except subprocess.TimeoutExpired:
            errors.append(f"repo-aware advisor exceeded the configured {args.timeout}-second timeout")
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

    attachment_marked = False
    attachment_mark_error = ""
    strict_attachment_evidence = not args.no_require_tool_activity and args.min_inspection_calls >= 1
    if not errors and strict_attachment_evidence:
        try:
            attachment_marked = mark_chatgpt_attachment_verified(project, state)
        except (OSError, RuntimeError) as exc:
            attachment_mark_error = safety.truncate(
                safety.redact_sensitive_text(str(exc)),
                300,
            )
        if not attachment_marked and not attachment_mark_error:
            attachment_mark_error = "connector changed or stopped before attachment verification was recorded"
    elif not errors:
        attachment_mark_error = "diagnostic tool-evidence relaxation cannot enable automatic agent routing"

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
        "chatgpt_attachment_marked": attachment_marked,
        "chatgpt_attachment_mark_error": attachment_mark_error,
    }
    safety.atomic_write_json(metadata_path, payload)
    request_payload.update(
        {
            "status": "completed" if not errors else "failed",
            "completed_utc": utc_now(),
        }
    )
    safety.atomic_write_json(request_path, request_payload)

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
        if attachment_mark_error:
            print(
                "Advisor agent warning: verified response accepted, but automatic agent routing "
                f"was not enabled: {attachment_mark_error}",
                file=sys.stderr,
            )
        print(f"\nAdvisor agent response saved: {response_path}", file=sys.stderr)
        print(f"Advisor agent metadata saved: {metadata_path}", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
