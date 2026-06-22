#!/usr/bin/env python3
"""Ask an external model for second-pass critique and answer-shaping guidance."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """You are an expert advisor helping Codex improve its answer before it is sent.

Answer naturally and directly. Give concise, actionable second-pass guidance
that helps Codex serve the user's real goal. Point out missing assumptions,
risks, tradeoffs, structural improvements, and concrete details to include when
they matter.

Do not expose private chain-of-thought. Do not invent facts. Do not force fixed
section headings; use headings or bullets only when they make the guidance
clearer. Do not rewrite the whole answer unless that is clearly the most useful
form of guidance.
"""

PROJECT_ID_RE = re.compile(r"(g-p-[A-Za-z0-9]+)")
THINKING_EFFORT_ALIASES = {
    "": None,
    "none": None,
    "off": None,
    "default": None,
    "instant": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "xhigh": "xhigh",
    "pro": "pro",
    "pro extended": "extended",
    "pro-extended": "extended",
    "pro_extended": "extended",
    "extended": "extended",
}
PRO_EXTENDED_ALIASES = {"pro extended", "pro-extended", "pro_extended"}
DEFAULT_MODEL = "gpt-5-5-thinking"
DEFAULT_PRO_EXTENDED_MODEL = "gpt-5-pro"


def system_prompt() -> str:
    return os.environ.get("ADVISOR_SYSTEM_PROMPT", SYSTEM_PROMPT)


def normalize_thinking_effort(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return THINKING_EFFORT_ALIASES.get(normalized, value.strip() or None)


def is_pro_extended_request(value: str | None) -> bool:
    return value is not None and value.strip().lower() in PRO_EXTENDED_ALIASES


def default_model_for(thinking_effort: str | None) -> str:
    if is_pro_extended_request(thinking_effort):
        return os.environ.get("ADVISOR_PRO_EXTENDED_MODEL", DEFAULT_PRO_EXTENDED_MODEL)
    return DEFAULT_MODEL


def configured_thinking_effort(reasoning_effort: str | None) -> str | None:
    del reasoning_effort
    explicit = (
        os.environ.get("ADVISOR_THINKING_EFFORT")
        or os.environ.get("ADVISOR_CHATGPT_THINKING_EFFORT")
        or os.environ.get("ADVISOR_INTELLIGENCE")
    )
    if explicit is not None:
        return normalize_thinking_effort(explicit)
    return None


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_text(path: str) -> str:
    return sanitize_text(Path(path).read_text(encoding="utf-8"))


def sanitize_text(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")


def redact_sensitive(text: str) -> str:
    patterns = [
        (r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", "[REDACTED_JWT]"),
        (r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}", "Bearer [REDACTED]"),
        (r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(access[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(refresh[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(session[_-]?id['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(cookie['\"]?\s*[:=]\s*['\"]?)[^'\"}]+", r"\1[REDACTED]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        detail = redact_sensitive(detail)
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {body[:500]}") from exc


def get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        detail = redact_sensitive(detail)
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {body[:500]}") from exc


def extract_responses_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text

    parts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) if parts else json.dumps(response, indent=2)


def extract_chat_text(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return json.dumps(response, indent=2)
    return content if isinstance(content, str) else json.dumps(content, indent=2)


def normalize_chatgpt_project_id(value: str | None) -> str | None:
    if not value:
        return None
    match = PROJECT_ID_RE.search(value)
    return match.group(1) if match else None


def bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def float_env(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def advisor_project_dir() -> Path:
    explicit = os.environ.get("ADVISOR_PROJECT_DIR")
    if explicit:
        return Path(explicit).resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def default_project_name() -> str:
    explicit = os.environ.get("ADVISOR_CHATGPT_PROJECT_NAME")
    if explicit and explicit.strip():
        return explicit.strip()
    name = advisor_project_dir().name.strip()
    return name or "Codex Advisor"


def project_binding_path() -> Path:
    return advisor_project_dir() / ".codex-advisor" / "project.json"


def read_project_binding() -> dict[str, Any]:
    path = project_binding_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Malformed advisor project binding at {path}. Repair or delete it before continuing.") from exc
    return data if isinstance(data, dict) else {}


def write_project_binding(project_id: str, source: str | None = None, name: str | None = None) -> None:
    path = project_binding_path()
    data = read_project_binding()
    data["chatgpt_project_id"] = project_id
    if source:
        data.setdefault("chatgpt_project_source", source)
    if name:
        data.setdefault("name", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def find_gizmo(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        gizmo = payload.get("gizmo")
        if isinstance(gizmo, dict) and isinstance(gizmo.get("id"), str):
            return gizmo
        for value in payload.values():
            found = find_gizmo(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_gizmo(value)
            if found:
                return found
    return None


def create_chatgpt_project(name: str, timeout: int) -> str | None:
    auth = load_chatgpt_auth()
    if not auth:
        return None
    headers = {
        **auth["headers"],
        "Content-Type": "application/json",
    }
    payload = {
        "files": [],
        "sharing": [{"type": "private"}],
        "instructions": "",
        "display": {
            "name": name,
            "description": f"Advisor project for {name}.",
        },
        "gizmo_type": "snorlax",
    }
    try:
        response = post_json("https://chatgpt.com/backend-api/gizmos/snorlax/upsert", payload, headers, timeout)
    except RuntimeError as exc:
        if bool_env("ADVISOR_PROJECT_CREATE_STRICT", False):
            raise
        print(f"Advisor project auto-create skipped: {redact_sensitive(str(exc))}", file=sys.stderr)
        return None
    gizmo = find_gizmo(response)
    if not gizmo:
        if bool_env("ADVISOR_PROJECT_CREATE_STRICT", False):
            raise RuntimeError("ChatGPT Project create response did not include a gizmo id.")
        print("Advisor project auto-create skipped: response did not include a project id.", file=sys.stderr)
        return None
    project_id = normalize_chatgpt_project_id(gizmo.get("id"))
    if not project_id:
        return None
    write_project_binding(project_id, "auto-created", name)
    print(f"Advisor ChatGPT Project auto-created: {name} ({project_id})", file=sys.stderr)
    return project_id


def chatgpt_project_id(timeout: int | None = None, allow_create: bool = True) -> str | None:
    explicit = (
        os.environ.get("ADVISOR_CHATGPT_PROJECT_ID")
        or os.environ.get("ADVISOR_GIZMO_ID")
        or os.environ.get("ADVISOR_CHATGPT_PROJECT_URL")
    )
    project_id = normalize_chatgpt_project_id(explicit)
    if project_id:
        write_project_binding(project_id, explicit, default_project_name())
        return project_id

    binding = read_project_binding()
    for key in ("chatgpt_project_id", "gizmo_id", "project_id", "chatgpt_project_url"):
        value = binding.get(key)
        if isinstance(value, str):
            project_id = normalize_chatgpt_project_id(value)
            if project_id:
                return project_id
    if allow_create and bool_env("ADVISOR_AUTO_CREATE_PROJECT", True):
        return create_chatgpt_project(default_project_name(), timeout or int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    return None


def default_state_path() -> Path:
    explicit = os.environ.get("ADVISOR_STATE_PATH")
    if explicit:
        return Path(explicit)
    key = os.environ.get("ADVISOR_CONVERSATION_KEY")
    if key:
        explicit_root = os.environ.get("ADVISOR_STATE_DIR")
        if explicit_root:
            root = Path(explicit_root)
        else:
            project_id = chatgpt_project_id(allow_create=False)
            root = advisor_project_dir() / ".codex-advisor"
            if project_id:
                root = root / "projects" / project_id
            root = root / "conversations"
        return root / f"{key}.conversation.json"
    project_id = chatgpt_project_id(allow_create=False)
    if project_id:
        return advisor_project_dir() / ".codex-advisor" / "projects" / project_id / "conversation.json"
    return advisor_project_dir() / ".codex-advisor" / "conversation.json"


def transcript_json_path(state_path: Path) -> Path:
    return state_path.with_name("transcript.json")


def transcript_md_path(state_path: Path) -> Path:
    return state_path.with_name("transcript.md")


def latest_response_path() -> Path:
    explicit = os.environ.get("ADVISOR_RESPONSE_PATH")
    if explicit:
        return Path(explicit)
    explicit_state = os.environ.get("ADVISOR_STATE_PATH")
    if explicit_state:
        return Path(explicit_state).with_name("latest-response.md")
    return default_state_path().with_name("latest-response.md")


def write_latest_response(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"Advisor latest-response write skipped: {redact_sensitive(str(exc))}", file=sys.stderr)


def load_conversation(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Malformed advisor conversation state at {path}. Repair or delete it before continuing.") from exc
    conversation = data.get("conversation")
    return conversation if isinstance(conversation, dict) else None


def saved_project_id(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Malformed advisor conversation state at {path}. Repair or delete it before continuing.") from exc
    value = data.get("chatgpt_project_id")
    return normalize_chatgpt_project_id(value) if isinstance(value, str) else None


def remove_state_files(state_path: Path) -> None:
    for path in (state_path, transcript_json_path(state_path), transcript_md_path(state_path)):
        path.unlink(missing_ok=True)


def save_conversation(path: Path, response: dict[str, Any], project_id: str | None = None) -> None:
    conversation = response.get("conversation")
    if not isinstance(conversation, dict):
        return
    payload: dict[str, Any] = {"conversation": conversation}
    if project_id:
        payload["chatgpt_project_id"] = project_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_skill_config() -> dict[str, Any]:
    config_path = Path(__file__).resolve().parents[1] / "advisor-config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def setup_dir_from_config() -> Path | None:
    explicit = os.environ.get("ADVISOR_SETUP_DIR")
    if explicit:
        return Path(explicit)
    config = read_skill_config()
    setup_dir = config.get("setup_dir")
    return Path(setup_dir) if isinstance(setup_dir, str) and setup_dir else None


def setup_dir_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = setup_dir_from_config()
    if configured:
        candidates.append(configured)
    candidates.append(Path(__file__).resolve().parents[3])
    candidates.append(Path.cwd())
    candidates.extend(Path.cwd().parents)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def auth_file_path() -> Path | None:
    explicit = os.environ.get("ADVISOR_AUTH_FILE")
    if explicit:
        return Path(explicit)
    for setup_dir in setup_dir_candidates():
        path = setup_dir / "vendor" / "gpt4free" / "har_and_cookies" / "auth_OpenaiChat.json"
        if path.exists():
            return path
    return None


def cookie_header(cookies: dict[str, Any]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if value is not None)


def load_chatgpt_auth() -> dict[str, Any] | None:
    path = auth_file_path()
    if not path or not path.exists():
        return None
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    api_key = auth.get("api_key")
    cookies = auth.get("cookies")
    if not isinstance(api_key, str) or not isinstance(cookies, dict):
        return None
    headers = auth.get("headers") if isinstance(auth.get("headers"), dict) else {}
    safe_headers = {
        "Authorization": f"Bearer {api_key}",
        "Cookie": cookie_header(cookies),
        "Accept": "application/json",
        "User-Agent": headers.get("user-agent", "Mozilla/5.0"),
    }
    return {"headers": safe_headers, "user_id": cookies.get("oai-did")}


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(part.get("content"), str):
                texts.append(part["content"])
    return "\n".join(text for text in texts if text)


def ordered_nodes(conversation_data: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = conversation_data.get("mapping")
    if not isinstance(mapping, dict):
        return []
    current = conversation_data.get("current_node") or conversation_data.get("current_node_id")
    if not current:
        candidates = [
            node for node in mapping.values()
            if isinstance(node, dict) and isinstance(node.get("message"), dict)
        ]
        candidates.sort(key=lambda node: node.get("message", {}).get("create_time") or 0)
        return candidates

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        node = mapping.get(current)
        if not isinstance(node, dict):
            break
        nodes.append(node)
        current = node.get("parent")
    nodes.reverse()
    return nodes


def transcript_from_conversation(conversation_data: dict[str, Any]) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    for node in ordered_nodes(conversation_data):
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        role = (message.get("author") or {}).get("role")
        text = message_text(message).strip()
        if role not in {"user", "assistant", "tool"} or not text:
            continue
        transcript.append({
            "id": message.get("id") or node.get("id"),
            "role": role,
            "create_time": message.get("create_time"),
            "status": message.get("status"),
            "content": text,
        })
    return transcript


def latest_message_id(conversation_data: dict[str, Any], transcript: list[dict[str, Any]]) -> str | None:
    current = conversation_data.get("current_node") or conversation_data.get("current_node_id")
    mapping = conversation_data.get("mapping")
    if isinstance(current, str) and isinstance(mapping, dict):
        node = mapping.get(current)
        if isinstance(node, dict):
            message = node.get("message")
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                return message["id"]
        return current
    if transcript:
        value = transcript[-1].get("id")
        return value if isinstance(value, str) else None
    return None


def latest_finished_assistant_text(conversation_data: dict[str, Any]) -> str:
    transcript = transcript_from_conversation(conversation_data)
    last_user_index = -1
    for index, item in enumerate(transcript):
        if item.get("role") == "user":
            last_user_index = index
    candidates = transcript[last_user_index + 1:] if last_user_index >= 0 else transcript
    for item in reversed(candidates):
        if item.get("role") == "assistant" and item.get("status") == "finished_successfully":
            content = str(item.get("content") or "").strip()
            if content:
                return content
    return ""


def write_transcript(state_path: Path, conversation_data: dict[str, Any], transcript: list[dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversation_id": conversation_data.get("conversation_id"),
        "title": conversation_data.get("title"),
        "current_node": conversation_data.get("current_node") or conversation_data.get("current_node_id"),
        "messages": transcript,
    }
    transcript_json_path(state_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        f"# Advisor Transcript",
        "",
        f"Conversation ID: {payload.get('conversation_id') or ''}",
        f"Title: {payload.get('title') or ''}",
        "",
    ]
    for item in transcript:
        role = str(item["role"]).title()
        lines.extend([f"## {role}", "", str(item["content"]).strip(), ""])
    transcript_md_path(state_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def sync_remote_conversation(
    state_path: Path,
    conversation: dict[str, Any] | None,
    timeout: int,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    if not conversation:
        return conversation
    conversation_id = conversation.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return conversation
    auth = load_chatgpt_auth()
    if not auth:
        return conversation
    url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
    try:
        conversation_data = get_json(url, auth["headers"], timeout)
    except RuntimeError as exc:
        if os.environ.get("ADVISOR_SYNC_STRICT", "false").lower() in ("1", "true", "yes"):
            raise
        print(f"Advisor remote sync skipped: {redact_sensitive(str(exc))}", file=sys.stderr)
        return conversation

    transcript = transcript_from_conversation(conversation_data)
    latest_id = latest_message_id(conversation_data, transcript)
    if latest_id:
        conversation["message_id"] = latest_id
        conversation["parent_message_id"] = latest_id
    if auth.get("user_id"):
        conversation["user_id"] = auth["user_id"]
    conversation["conversation_id"] = conversation_id
    write_transcript(state_path, conversation_data, transcript)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"conversation": conversation}
    if project_id:
        payload["chatgpt_project_id"] = project_id
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return conversation


def fetch_remote_final_text(
    state_path: Path,
    conversation: dict[str, Any] | None,
    timeout: int,
    project_id: str | None = None,
) -> str:
    if not conversation:
        return ""
    conversation_id = conversation.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return ""
    auth = load_chatgpt_auth()
    if not auth:
        return ""
    max_polls = int_env("ADVISOR_FINAL_FETCH_MAX_POLLS", 1, minimum=1)
    fallback_timeout = int_env("ADVISOR_FINAL_FETCH_TIMEOUT", min(max(timeout, 1), 180), minimum=1)
    poll_seconds = float_env("ADVISOR_FINAL_FETCH_POLL_SECONDS", 5.0, minimum=0.5)
    deadline = time.monotonic() + fallback_timeout
    url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
    last_data: dict[str, Any] | None = None
    for attempt in range(max_polls):
        try:
            conversation_data = get_json(url, auth["headers"], timeout)
        except RuntimeError as exc:
            if os.environ.get("ADVISOR_SYNC_STRICT", "false").lower() in ("1", "true", "yes"):
                raise
            print(f"Advisor final fetch skipped: {redact_sensitive(str(exc))}", file=sys.stderr)
            return ""
        last_data = conversation_data
        text = latest_finished_assistant_text(conversation_data)
        if text:
            transcript = transcript_from_conversation(conversation_data)
            latest_id = latest_message_id(conversation_data, transcript)
            if latest_id:
                conversation["message_id"] = latest_id
                conversation["parent_message_id"] = latest_id
            if auth.get("user_id"):
                conversation["user_id"] = auth["user_id"]
            conversation["conversation_id"] = conversation_id
            write_transcript(state_path, conversation_data, transcript)
            payload: dict[str, Any] = {"conversation": conversation}
            if project_id:
                payload["chatgpt_project_id"] = project_id
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return text
        if attempt + 1 >= max_polls or time.monotonic() >= deadline:
            if last_data is not None:
                transcript = transcript_from_conversation(last_data)
                if transcript:
                    write_transcript(state_path, last_data, transcript)
            return ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        time.sleep(min(poll_seconds, remaining))
    return ""


def build_prompt(prompt: str, context_files: list[str]) -> str:
    blocks = [prompt.strip()]
    for path in context_files:
        blocks.append(f"\n\n--- Context file: {path} ---\n{read_text(path)}")
    return "\n".join(block for block in blocks if block.strip())


def call_openai(prompt: str, model: str, timeout: int) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when ADVISOR_PROVIDER=openai")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "reasoning": {"effort": os.environ.get("ADVISOR_REASONING_EFFORT", "high")},
        "max_output_tokens": int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1800")),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return extract_responses_text(post_json(f"{base_url}/responses", payload, headers, timeout))


def call_compatible(prompt: str, model: str, timeout: int) -> str:
    base_url = os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    api_key = os.environ.get("ADVISOR_API_KEY", os.environ.get("OPENAI_API_KEY", "local"))
    reasoning_effort = os.environ.get("ADVISOR_REASONING_EFFORT")
    thinking_effort = configured_thinking_effort(reasoning_effort)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1800")),
    }
    persist = os.environ.get("ADVISOR_PERSIST_CONVERSATION", "true").lower() in ("1", "true", "yes")
    temporary = os.environ.get("ADVISOR_TEMPORARY", "false").lower() in ("1", "true", "yes")
    sync_remote = os.environ.get("ADVISOR_SYNC_REMOTE", "true").lower() in ("1", "true", "yes")
    project_id = chatgpt_project_id(timeout, allow_create=(persist and not temporary))
    if project_id:
        payload["gizmo_id"] = project_id
    conversation = None
    if persist:
        state_path = default_state_path()
        conversation = load_conversation(state_path)
        previous_project_id = saved_project_id(state_path)
        if previous_project_id and project_id and previous_project_id != project_id:
            raise RuntimeError(
                f"Advisor state at {state_path} belongs to {previous_project_id}, "
                f"but the active ChatGPT Project is {project_id}. "
                "Clear or migrate this state before continuing."
            )
        if sync_remote and not temporary:
            conversation = sync_remote_conversation(state_path, conversation, timeout, project_id)
        if conversation:
            payload["conversation"] = conversation
    else:
        state_path = None
    if temporary:
        payload["temporary"] = True
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if thinking_effort:
        payload["thinking_effort"] = thinking_effort
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = post_json(f"{base_url}/chat/completions", payload, headers, timeout)
    except RuntimeError as exc:
        stale_markers = ("conversation_deleted", "conversation_not_found", "conversation_inaccessible")
        if persist and conversation and any(marker in str(exc) for marker in stale_markers):
            if state_path is not None:
                remove_state_files(state_path)
            payload.pop("conversation", None)
            response = post_json(f"{base_url}/chat/completions", payload, headers, timeout)
        else:
            raise
    if persist and state_path is not None:
        save_conversation(state_path, response, project_id)
        if sync_remote and not temporary:
            sync_remote_conversation(state_path, load_conversation(state_path), timeout, project_id)
    text = extract_chat_text(response)
    if thinking_effort and not text.strip():
        if persist and state_path is not None:
            text = fetch_remote_final_text(state_path, load_conversation(state_path), timeout, project_id)
    if thinking_effort and not text.strip():
        raise RuntimeError(
            f"ChatGPT returned an empty response for thinking_effort={thinking_effort!r}. "
            "Refresh the HAR/session first; if it still fails, inspect the saved transcript "
            "and the g4f/OpenaiChat conversation-turn WebSocket handoff used by Pro/extended thinking turns."
        )
    return text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Prompt, question, draft, or plan. Reads stdin when omitted.")
    parser.add_argument("--context-file", action="append", default=[], help="Additional UTF-8 context file.")
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL"))
    parser.add_argument(
        "--thinking-effort",
        default=(
            os.environ.get("ADVISOR_THINKING_EFFORT")
            or os.environ.get("ADVISOR_CHATGPT_THINKING_EFFORT")
            or os.environ.get("ADVISOR_INTELLIGENCE")
        ),
        help="ChatGPT web intelligence/thinking effort, e.g. high, xhigh, pro-extended, or extended.",
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "openai-compatible"],
        default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"),
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--save", help="Optional file path to write the guidance.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    if args.thinking_effort is not None:
        os.environ["ADVISOR_THINKING_EFFORT"] = args.thinking_effort
    if args.model is None:
        args.model = default_model_for(args.thinking_effort)
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    prompt = sanitize_text(build_prompt(prompt, args.context_file))
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2

    guidance = call_openai(prompt, args.model, args.timeout) if args.provider == "openai" else call_compatible(prompt, args.model, args.timeout)

    if args.save:
        Path(args.save).write_text(guidance, encoding="utf-8")
    write_latest_response(latest_response_path(), guidance)
    print(guidance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
