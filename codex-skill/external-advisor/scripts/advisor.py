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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import activity_monitor
import advisor_concurrency as concurrency
import advisor_safety as safety


SYSTEM_PROMPT = """You are an expert advisor helping Codex improve its answer before it is sent.

Answer naturally and directly. Give concise, actionable second-pass guidance
that helps Codex serve the user's real goal. Point out missing assumptions,
risks, tradeoffs, structural improvements, and concrete details to include when
they matter.

You do not have implicit access to Codex's repository, filesystem, terminal,
git state, logs, tests, screenshots, or local observations. You only know what
Codex includes in this prompt or attached context. Do not imply that you have
inspected files, commands, errors, or runtime state unless their contents were
provided. Treat file names, modules, metrics, commands, and root causes not
present in the prompt as hypotheses for Codex to verify locally.

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
    "light": "min",
    "min": "min",
    "minimum": "min",
    "low": "min",
    "medium": "standard",
    "standard": "standard",
    "thinking": "standard",
    "high": "extended",
    "extended thinking": "extended",
    "extra high": "max",
    "extra-high": "max",
    "extra_high": "max",
    "xhigh": "max",
    "reasoning high": "extended",
    "reasoning-high": "extended",
    "reasoning_high": "extended",
    "reasoning xhigh": "max",
    "reasoning-xhigh": "max",
    "reasoning_xhigh": "max",
    "reasoning extra high": "max",
    "reasoning-extra-high": "max",
    "reasoning_extra_high": "max",
    "heavy": "max",
    "max": "max",
    "maximum": "max",
    "pro": "standard",
    "pro standard": "standard",
    "pro-standard": "standard",
    "pro_standard": "standard",
    "pro extended": "standard",
    "pro-extended": "standard",
    "pro_extended": "standard",
    "extended": "extended",
    "ultra": "ultra",
}
PRO_STANDARD_ALIASES = {"pro", "pro standard", "pro-standard", "pro_standard"}
PRO_EXTENDED_ALIASES = {"pro extended", "pro-extended", "pro_extended"}
DEFAULT_MODEL = "gpt-5-6-thinking"
SAFE_NON_THINKING_MODEL = "gpt-5-5"
DEFAULT_CHATGPT_THINKING_EFFORT = "max"
DEFAULT_PRO_EXTENDED_MODEL = "gpt-5-6-pro"
ALLOW_NON_DEFAULT_ROUTE_ENV = "ADVISOR_ALLOW_NON_DEFAULT_ROUTE"
LEGACY_THINKING_MODELS = {"gpt-5-5-thinking", "gpt-5.5-thinking", "gpt-5_5-thinking"}
COMPATIBLE_MODEL_ALIASES = {
    "gpt-5-6-thinking": ("gpt-5.6", "gpt-5-6", "gpt-5_6", "gpt-5.6-sol", "gpt-5-6-sol", "gpt-5_6_sol", "gpt-5.6-sol-wm"),
    "gpt-5-6-pro": ("gpt-5.6-pro", "gpt-5_6_pro"),
    "gpt-5.6-terra-wm": ("gpt-5.6-terra", "gpt-5-6-terra", "gpt-5_6_terra"),
    "gpt-5.6-luna-wm": ("gpt-5.6-luna", "gpt-5-6-luna", "gpt-5_6_luna"),
    "gpt-5-5-thinking": ("gpt-5.5-thinking", "gpt-5_5-thinking"),
    "gpt-5-5-pro": ("gpt-5.5-pro", "gpt-5_5-pro"),
    "gpt-5-5": ("gpt-5.5", "gpt-5_5"),
}
COMPATIBLE_MODEL_FALLBACKS = (
    "gpt-5-6-thinking",
    "gpt-5.6-sol",
    "gpt-5.6",
    "gpt-5-5-thinking",
    "gpt-5.5-thinking",
    "gpt-5_5-thinking",
)


def system_prompt() -> str:
    return os.environ.get("ADVISOR_SYSTEM_PROMPT", SYSTEM_PROMPT)


def normalize_thinking_effort(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in THINKING_EFFORT_ALIASES:
        return THINKING_EFFORT_ALIASES[normalized]
    if bool_env("ADVISOR_ALLOW_UNKNOWN_THINKING_EFFORT", False):
        return value.strip() or None
    allowed = ", ".join(sorted(key for key in THINKING_EFFORT_ALIASES if key))
    raise RuntimeError(
        f"Unknown ADVISOR_THINKING_EFFORT value {value!r}. "
        f"Known values/aliases: {allowed}. Set ADVISOR_ALLOW_UNKNOWN_THINKING_EFFORT=true "
        "only for deliberate diagnostics against a changed ChatGPT web schema."
    )


def is_pro_extended_request(value: str | None) -> bool:
    return value is not None and value.strip().lower() in PRO_EXTENDED_ALIASES


def is_pro_request(value: str | None) -> bool:
    return value is not None and value.strip().lower() in (PRO_STANDARD_ALIASES | PRO_EXTENDED_ALIASES)


def allow_non_default_route() -> bool:
    return bool_env(ALLOW_NON_DEFAULT_ROUTE_ENV, False)


def default_model_for(thinking_effort: str | None) -> str:
    if is_pro_request(thinking_effort):
        return os.environ.get("ADVISOR_PRO_EXTENDED_MODEL", DEFAULT_PRO_EXTENDED_MODEL)
    return DEFAULT_MODEL


def select_request_thinking_effort(thinking_effort: str | None) -> str | None:
    if is_pro_request(thinking_effort) or allow_non_default_route():
        return thinking_effort

    normalized_effort = normalize_thinking_effort(thinking_effort)
    if thinking_effort is not None and normalized_effort != DEFAULT_CHATGPT_THINKING_EFFORT:
        print(
            "Advisor forcing non-Pro ADVISOR_THINKING_EFFORT to "
            f"{DEFAULT_CHATGPT_THINKING_EFFORT!r} instead of {thinking_effort!r}. "
            "Use ADVISOR_THINKING_EFFORT=pro-extended for Pro, or set "
            f"{ALLOW_NON_DEFAULT_ROUTE_ENV}=true only for deliberate diagnostics.",
            file=sys.stderr,
        )
    return DEFAULT_CHATGPT_THINKING_EFFORT


def configured_thinking_effort(reasoning_effort: str | None) -> str | None:
    del reasoning_effort
    explicit = (
        os.environ.get("ADVISOR_THINKING_EFFORT")
        or os.environ.get("ADVISOR_CHATGPT_THINKING_EFFORT")
        or os.environ.get("ADVISOR_INTELLIGENCE")
    )
    if explicit is not None:
        if not is_pro_request(explicit) and not allow_non_default_route():
            return DEFAULT_CHATGPT_THINKING_EFFORT
        return normalize_thinking_effort(explicit)
    default = os.environ.get("ADVISOR_DEFAULT_THINKING_EFFORT", DEFAULT_CHATGPT_THINKING_EFFORT)
    if default is not None and not allow_non_default_route():
        return DEFAULT_CHATGPT_THINKING_EFFORT
    return normalize_thinking_effort(default) if default is not None else None


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_text(path: str) -> str:
    return safety.read_limited_text(Path(path), redact=True)


def sanitize_text(text: str) -> str:
    return safety.sanitize_text(text)


def redact_sensitive(text: str) -> str:
    return safety.redact_sensitive_text(text)


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
        preview = safety.truncate(redact_sensitive(body), 500)
        raise RuntimeError(f"Non-JSON response from {url}: {preview}") from exc


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
        preview = safety.truncate(redact_sensitive(body), 500)
        raise RuntimeError(f"Non-JSON response from {url}: {preview}") from exc


def compatible_model_ids(base_url: str, headers: dict[str, str], timeout: int) -> set[str]:
    payload = get_json(f"{base_url}/models", headers, min(timeout, 5))
    models = payload.get("data")
    if not isinstance(models, list):
        return set()
    ids: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if isinstance(model_id, str) and model_id.strip():
            ids.add(model_id.strip())
    return ids


def g4f_provider_base_url(base_url: str) -> str | None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def g4f_provider_model_ids(base_url: str, headers: dict[str, str], timeout: int) -> tuple[set[str], str] | None:
    provider_root = g4f_provider_base_url(base_url)
    if provider_root is None:
        return None
    provider = (
        os.environ.get("ADVISOR_G4F_PROVIDER")
        or os.environ.get("G4F_PROVIDER")
        or "OpenaiAccount"
    ).strip()
    if not provider:
        return None
    quoted_provider = urllib.parse.quote(provider, safe="")
    url = f"{provider_root}/api/{quoted_provider}/models"
    try:
        return compatible_model_ids(url.rsplit("/models", 1)[0], headers, timeout), url
    except RuntimeError:
        return None


def resolve_compatible_model(model: str, base_url: str, headers: dict[str, str], timeout: int) -> str:
    if not bool_env("ADVISOR_VALIDATE_MODEL", True):
        return model
    try:
        provider_models = g4f_provider_model_ids(base_url, headers, timeout)
        if provider_models is not None:
            models, model_source = provider_models
        else:
            models = compatible_model_ids(base_url, headers, timeout)
            model_source = f"{base_url}/models"
    except RuntimeError as exc:
        print(f"Advisor model validation skipped: {exc}", file=sys.stderr)
        return model
    if not models or model in models:
        return model
    for alias in COMPATIBLE_MODEL_ALIASES.get(model, ()):
        if alias in models:
            print(
                f"Advisor requested model {model!r}; {model_source} exposes alias {alias!r}.",
                file=sys.stderr,
            )
            return alias
    if not bool_env("ADVISOR_ALLOW_MODEL_FALLBACK", False):
        raise RuntimeError(
            f"Advisor requested model {model!r}, but {model_source} does not expose it. "
            "Refusing to fall back automatically because that can create a weaker ChatGPT model chat. "
            "Refresh/update the local g4f/HAR model list, pass the exact ChatGPT web model slug with ADVISOR_MODEL, "
            "or set ADVISOR_VALIDATE_MODEL=false only for a deliberate diagnostic."
        )
    for fallback in COMPATIBLE_MODEL_FALLBACKS:
        if fallback in models:
            print(
                f"Advisor requested model {model!r} is not exposed by {model_source}; using {fallback!r}.",
                file=sys.stderr,
            )
            return fallback
    available = ", ".join(sorted(models)[:12])
    raise RuntimeError(
        f"Advisor requested model {model!r} is not exposed by {model_source}, "
        f"and no preferred fallback is available. First available models: {available}"
    )


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


def is_pro_model_slug(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in {
        "gpt-5-5-pro",
        "gpt-5.5-pro",
        "gpt-5_5-pro",
    } or normalized.endswith("-pro")


def select_request_model(thinking_effort: str | None, model: str | None) -> str:
    normalized_effort = normalize_thinking_effort(thinking_effort)
    if isinstance(model, str):
        model = model.strip() or None
    if isinstance(model, str) and model.lower() == "default":
        print(
            "Advisor ignoring ADVISOR_MODEL='default' and selecting the configured safe model "
            "for the requested thinking mode.",
            file=sys.stderr,
        )
        model = None
    if not is_pro_request(thinking_effort):
        if not allow_non_default_route():
            if model is not None and model != DEFAULT_MODEL:
                print(
                    "Advisor ignoring non-Pro ADVISOR_MODEL="
                    f"{model!r} and using {DEFAULT_MODEL!r} with "
                    f"thinking_effort={DEFAULT_CHATGPT_THINKING_EFFORT!r}. "
                    f"Set {ALLOW_NON_DEFAULT_ROUTE_ENV}=true only for deliberate diagnostics.",
                    file=sys.stderr,
                )
            return DEFAULT_MODEL
        selected = model or DEFAULT_MODEL
        explicit_no_thinking = thinking_effort is not None and normalized_effort is None
        if (
            selected in LEGACY_THINKING_MODELS
            and (explicit_no_thinking or normalized_effort in {"min", "standard"})
            and not bool_env("ADVISOR_ALLOW_LEGACY_THINKING_MODEL", False)
        ):
            print(
                f"Advisor replacing legacy Thinking model {selected!r} with {SAFE_NON_THINKING_MODEL!r}; "
                "current ChatGPT metadata resolves that legacy route to gpt-5-3-mini unless "
                "thinking_effort is extended/max. Set ADVISOR_ALLOW_LEGACY_THINKING_MODEL=true "
                "only for deliberate diagnostics.",
                file=sys.stderr,
            )
            return SAFE_NON_THINKING_MODEL
        return selected

    pro_model = default_model_for(thinking_effort)
    if model is None or model == pro_model:
        return pro_model
    if bool_env("ADVISOR_ALLOW_PRO_MODEL_OVERRIDE", False):
        print(
            f"Advisor Pro/Pro Extended using explicit model override {model!r}; expected {pro_model!r}.",
            file=sys.stderr,
        )
        return model

    if model in {DEFAULT_MODEL, *LEGACY_THINKING_MODELS}:
        print(
            f"Advisor Pro/Pro Extended overriding normal/legacy model {model!r} with {pro_model!r}. "
            "Set ADVISOR_ALLOW_PRO_MODEL_OVERRIDE=true only for deliberate diagnostics.",
            file=sys.stderr,
        )
        return pro_model

    raise RuntimeError(
        f"Refusing Pro/Pro Extended with model {model!r}; expected {pro_model!r}. "
        "Use ADVISOR_PRO_EXTENDED_MODEL to update the ChatGPT Pro slug, or set "
        "ADVISOR_ALLOW_PRO_MODEL_OVERRIDE=true only for a deliberate diagnostic."
    )


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
    safety.atomic_write_json(path, data)


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


def find_conversation_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        conversation = payload.get("conversation")
        if isinstance(conversation, dict) and isinstance(conversation.get("conversation_id"), str):
            return conversation
        if isinstance(payload.get("conversation_id"), str):
            return payload
        for value in payload.values():
            found = find_conversation_payload(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_conversation_payload(value)
            if found:
                return found
    return None


def find_conversation_data_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("mapping"), dict):
            return payload
        for value in payload.values():
            found = find_conversation_data_payload(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_conversation_data_payload(value)
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
        if allow_create:
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
        key = safety.safe_key_slug(key, default="conversation")
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
    name = state_path.name
    if name.endswith(".conversation.json") and name != "conversation.json":
        return state_path.with_name(name[: -len(".conversation.json")] + ".transcript.json")
    return state_path.with_name("transcript.json")


def transcript_md_path(state_path: Path) -> Path:
    name = state_path.name
    if name.endswith(".conversation.json") and name != "conversation.json":
        return state_path.with_name(name[: -len(".conversation.json")] + ".transcript.md")
    return state_path.with_name("transcript.md")


def latest_response_path() -> Path:
    explicit = os.environ.get("ADVISOR_RESPONSE_PATH")
    if explicit:
        return Path(explicit)
    explicit_state = os.environ.get("ADVISOR_STATE_PATH")
    if explicit_state:
        return Path(explicit_state).with_name("latest-response.md")
    return default_state_path().with_name("latest-response.md")


def latest_response_paths() -> list[Path]:
    primary = latest_response_path()
    if os.environ.get("ADVISOR_RESPONSE_PATH") or os.environ.get("ADVISOR_STATE_PATH"):
        return [primary]
    root_latest = advisor_project_dir() / ".codex-advisor" / "latest-response.md"
    if primary.resolve() == root_latest.resolve():
        return [primary]
    return [primary, root_latest]


def write_latest_response(path: Path, text: str) -> bool:
    try:
        safety.atomic_write_text(path, text)
        return True
    except OSError as exc:
        print(f"Advisor latest-response write skipped: {redact_sensitive(str(exc))}", file=sys.stderr)
        return False


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
    for path in (
        state_path,
        transcript_json_path(state_path),
        transcript_md_path(state_path),
        state_path.with_name("latest-response.md"),
    ):
        path.unlink(missing_ok=True)


def stale_conversation_error(exc: RuntimeError) -> bool:
    text = str(exc)
    markers = (
        "HTTP 404",
        "HTTP 410",
        "conversation_deleted",
        "conversation_not_found",
    )
    return any(marker in text for marker in markers)


def save_conversation(path: Path, response: dict[str, Any], project_id: str | None = None) -> None:
    conversation = find_conversation_payload(response)
    if conversation is None:
        return
    payload: dict[str, Any] = {"conversation": conversation}
    if project_id:
        payload["chatgpt_project_id"] = project_id
    safety.atomic_write_json(path, payload)


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
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and (
            metadata.get("is_visually_hidden_from_conversation")
            or metadata.get("is_visually_hidden_from_conversation_history")
        ):
            continue
        role = (message.get("author") or {}).get("role")
        text = message_text(message).strip()
        if role not in {"user", "assistant", "tool"} or not text:
            continue
        item: dict[str, Any] = {
            "id": message.get("id") or node.get("id"),
            "role": role,
            "create_time": message.get("create_time"),
            "status": message.get("status"),
            "content": text,
        }
        if "end_turn" in message:
            item["end_turn"] = message.get("end_turn")
        if isinstance(message.get("recipient"), str):
            item["recipient"] = message["recipient"]
        transcript.append(item)
        if isinstance(metadata, dict):
            model_metadata = {
                key: metadata.get(key)
                for key in (
                    "model_slug",
                    "requested_model_slug",
                    "resolved_model_slug",
                    "default_model_slug",
                    "thinking_effort",
                )
                if isinstance(metadata.get(key), (str, int, float, bool))
            }
            if model_metadata:
                transcript[-1]["metadata"] = model_metadata
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


def latest_finished_assistant_message_id(conversation_data: dict[str, Any]) -> str | None:
    transcript = transcript_from_conversation(conversation_data)
    last_user_index = -1
    for index, item in enumerate(transcript):
        if item.get("role") == "user":
            last_user_index = index
    candidates = transcript[last_user_index + 1:] if last_user_index >= 0 else transcript
    for item in reversed(candidates):
        if item.get("role") != "assistant" or not assistant_item_has_final_content(item):
            continue
        content = str(item.get("content") or "").strip()
        message_id = item.get("id")
        if content and isinstance(message_id, str):
            return message_id
    return None


def latest_finished_assistant_text(conversation_data: dict[str, Any]) -> str:
    transcript = transcript_from_conversation(conversation_data)
    last_user_index = -1
    for index, item in enumerate(transcript):
        if item.get("role") == "user":
            last_user_index = index
    candidates = transcript[last_user_index + 1:] if last_user_index >= 0 else transcript
    for item in reversed(candidates):
        if item.get("role") == "assistant" and assistant_item_has_final_content(item):
            content = str(item.get("content") or "").strip()
            if content:
                return content
    return ""


def latest_finished_assistant_text_for_prompt_data(conversation_data: dict[str, Any], prompt: str) -> str:
    transcript = transcript_from_conversation(conversation_data)
    return latest_assistant_text_after_prompt_messages(transcript, prompt)


def latest_assistant_text_after_prompt_messages(messages: list[Any], prompt: str) -> str:
    normalized_prompt = prompt.strip()
    last_user_index = -1
    for index, item in enumerate(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        if str(item.get("content") or "").strip() == normalized_prompt:
            last_user_index = index
    if last_user_index < 0:
        return ""
    candidates: list[dict[str, Any]] = []
    for item in messages[last_user_index + 1:]:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            break
        if item.get("role") == "assistant":
            candidates.append(item)
    for item in reversed(candidates):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        if not assistant_item_has_final_content(item):
            continue
        content = str(item.get("content") or "").strip()
        if content:
            return content
    return ""


def transcript_contains_prompt(messages: list[Any], prompt: str) -> bool:
    normalized_prompt = prompt.strip()
    return any(
        isinstance(item, dict)
        and item.get("role") == "user"
        and str(item.get("content") or "").strip() == normalized_prompt
        for item in messages
    )


def latest_transcript_assistant_text_for_prompt(state_path: Path, prompt: str) -> str:
    path = transcript_json_path(state_path)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return ""
    return latest_assistant_text_after_prompt_messages(messages, prompt)


def transcript_has_unfinished_assistant_after_prompt(state_path: Path, prompt: str) -> bool:
    path = transcript_json_path(state_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    normalized_prompt = prompt.strip()
    last_user_index = -1
    for index, item in enumerate(messages):
        if (
            isinstance(item, dict)
            and item.get("role") == "user"
            and str(item.get("content") or "").strip() == normalized_prompt
        ):
            last_user_index = index
    if last_user_index < 0:
        return False
    for item in messages[last_user_index + 1:]:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            break
        if item.get("role") == "assistant" and str(item.get("content") or "").strip():
            if not assistant_item_has_final_content(item):
                return True
    return False


def latest_assistant_metadata_after_prompt_messages(messages: list[Any], prompt: str) -> dict[str, Any]:
    normalized_prompt = prompt.strip()
    last_user_index = -1
    for index, item in enumerate(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        if str(item.get("content") or "").strip() == normalized_prompt:
            last_user_index = index
    if last_user_index < 0:
        return {}
    candidates: list[dict[str, Any]] = []
    for item in messages[last_user_index + 1:]:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            break
        if item.get("role") == "assistant":
            candidates.append(item)
    for item in reversed(candidates):
        if not assistant_item_has_final_content(item):
            continue
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            return metadata
    return {}


def latest_transcript_assistant_metadata_for_prompt(state_path: Path, prompt: str) -> dict[str, Any]:
    path = transcript_json_path(state_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    messages = data.get("messages")
    if not isinstance(messages, list):
        return {}
    return latest_assistant_metadata_after_prompt_messages(messages, prompt)


def rejected_resolved_model_slugs() -> set[str]:
    raw = os.environ.get("ADVISOR_REJECT_RESOLVED_MODEL_SLUGS", "gpt-5-3-mini")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def metadata_pro_request_model(metadata: dict[str, Any]) -> str | None:
    for key in ("requested_model_slug", "model_slug", "default_model_slug"):
        value = metadata.get(key)
        if is_pro_model_slug(value):
            return str(value)
    return None


def metadata_matches_current_pro_effort(metadata: dict[str, Any]) -> bool:
    requested = os.environ.get("ADVISOR_THINKING_EFFORT")
    if not is_pro_request(requested):
        return False
    expected = normalize_thinking_effort(requested)
    actual = metadata.get("thinking_effort")
    if actual is None:
        return True
    return str(actual).strip().lower() == expected


def metadata_is_current_pro_request(metadata: dict[str, Any]) -> bool:
    return bool(metadata_pro_request_model(metadata)) and metadata_matches_current_pro_effort(metadata)


def assert_resolved_model_route(state_path: Path | None, prompt: str) -> None:
    if bool_env("ADVISOR_ALLOW_RESOLVED_MODEL_DOWNGRADE", False):
        return
    if state_path is None:
        return
    metadata = latest_transcript_assistant_metadata_for_prompt(state_path, prompt)
    if not metadata:
        return
    resolved = metadata.get("resolved_model_slug")
    if isinstance(resolved, str) and resolved.strip().lower() in rejected_resolved_model_slugs():
        if metadata_is_current_pro_request(metadata):
            # Browser-captured Pro Extended turns can still report gpt-5-3-mini
            # here; the Pro request slug plus extended effort are the stable
            # signal for this route.
            print(
                "Advisor Pro/Pro Extended metadata contains a rejected-looking resolved_model_slug, "
                "but the latest browser Pro Extended HAR uses the same resolved slug while keeping "
                "model_slug/default_model_slug on the Pro model. Accepting the Pro-shaped route: "
                f"metadata={metadata!r}",
                file=sys.stderr,
            )
            return
        raise RuntimeError(
            "Advisor request resolved to a rejected/weaker ChatGPT model. "
            f"metadata={metadata!r}. Refresh the HAR/session or choose a route that does not "
            "downgrade. Set ADVISOR_ALLOW_RESOLVED_MODEL_DOWNGRADE=true only for deliberate diagnostics."
        )


def assert_pro_model_route(state_path: Path | None, prompt: str) -> None:
    if not is_pro_request(os.environ.get("ADVISOR_THINKING_EFFORT")):
        return
    if state_path is None:
        return
    metadata = latest_transcript_assistant_metadata_for_prompt(state_path, prompt)
    if not metadata:
        return
    resolved = (
        metadata.get("resolved_model_slug")
        or metadata.get("requested_model_slug")
        or metadata.get("model_slug")
    )
    if is_pro_model_slug(resolved):
        return
    pro_request_model = metadata_pro_request_model(metadata)
    if pro_request_model and metadata_matches_current_pro_effort(metadata):
        print(
            "Advisor Pro/Pro Extended route accepted from Pro-shaped assistant metadata. "
            "ChatGPT may report a non-Pro resolved_model_slug even for browser-captured Pro Extended turns: "
            f"metadata={metadata!r}",
            file=sys.stderr,
        )
        return
    raise RuntimeError(
        "Advisor Pro/Pro Extended request did not resolve to a Pro model. "
        f"metadata={metadata!r}. This usually means a normal ADVISOR_MODEL override, "
        "a stale g4f runtime patch, a ChatGPT web model slug change, or a HAR/session "
        "that does not contain/allow the real Pro browser route."
    )


def assistant_status_has_final_content(status: Any) -> bool:
    if status is None:
        return True
    normalized = str(status).strip().lower()
    if not normalized:
        return True
    if normalized in {
        "finished_successfully",
        "finished",
        "complete",
        "completed",
        "success",
        "succeeded",
    }:
        return True
    if normalized in {
        "in_progress",
        "running",
        "pending",
        "queued",
        "failed",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "interrupted",
    }:
        return False
    return not any(marker in normalized for marker in ("progress", "running", "pending", "fail", "error", "cancel"))


def assistant_item_has_final_content(item: dict[str, Any]) -> bool:
    # ChatGPT agent turns emit several visible progress messages whose status is
    # "finished_successfully" even though the overall turn is still running.
    # Current remote payloads distinguish those messages with end_turn=false.
    # Older saved transcripts do not have the field, so retain status-only
    # compatibility for them.
    if "end_turn" in item and item.get("end_turn") is not True:
        return False
    return assistant_status_has_final_content(item.get("status"))


def exact_repeated_half(text: str) -> str:
    stripped = text.strip()
    if len(stripped) < 8 or len(stripped) % 2 != 0:
        return ""
    midpoint = len(stripped) // 2
    first = stripped[:midpoint]
    second = stripped[midpoint:]
    if first and first == second:
        return first
    return ""


def synced_text_matches_repeated_local(local_text: str, synced_text: str) -> bool:
    repeated = exact_repeated_half(local_text)
    if not repeated:
        return False
    return repeated.strip() == synced_text.strip()


def should_prefer_synced_text(local_text: str, synced_text: str) -> bool:
    local = local_text.strip()
    synced = synced_text.strip()
    if not synced:
        return False
    if not local:
        return True
    if local == synced:
        return False
    if synced_text_matches_repeated_local(local, synced):
        return True
    if len(synced) < 200:
        return False
    if local in synced and len(local) < len(synced):
        return True
    return len(local) < 200 and len(synced) >= max(len(local) * 3, len(local) + 200)


def looks_like_tail_fragment(text: str, prompt: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    prompt_min = int_env("ADVISOR_TAIL_FRAGMENT_PROMPT_MIN_CHARS", 300, minimum=1)
    if len(prompt.strip()) < prompt_min:
        return False
    max_fragment = int_env("ADVISOR_TAIL_FRAGMENT_MAX_CHARS", 240, minimum=20)
    if len(stripped) > max_fragment:
        return False
    if text[:1].isspace():
        return True
    if stripped[:1].islower():
        return True
    if stripped.endswith("**") or stripped.startswith(("...", ",", ".", ";", ":", ")")):
        return True
    return False


def looks_suspiciously_short(text: str, prompt: str, *, had_transport_corruption: bool = False) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    prompt_min = int_env("ADVISOR_SUSPICIOUS_SHORT_PROMPT_MIN_CHARS", 300, minimum=1)
    if len(prompt.strip()) < prompt_min:
        return False
    max_chars = int_env("ADVISOR_SUSPICIOUS_SHORT_MAX_CHARS", 120, minimum=20)
    if len(stripped) > max_chars:
        return False
    if bool_env("ADVISOR_ALLOW_SUSPICIOUS_SHORT_RESPONSE", False):
        return False
    return had_transport_corruption or looks_like_tail_fragment(stripped, prompt)


def response_needs_remote_recovery(text: str, prompt: str, *, had_transport_corruption: bool = False) -> bool:
    return not text.strip() or looks_like_tail_fragment(text, prompt) or looks_suspiciously_short(
        text,
        prompt,
        had_transport_corruption=had_transport_corruption,
    )


def recovery_debug_context(state_path: Path | None = None) -> str:
    parts = [
        f"auth_file={auth_file_path() or 'not-found'}",
        f"setup_dir={setup_dir_from_config() or 'not-configured'}",
    ]
    if state_path is not None:
        parts.extend([
            f"state_path={state_path}",
            f"transcript_json={transcript_json_path(state_path)}",
            f"transcript_md={transcript_md_path(state_path)}",
        ])
    parts.append("latest_response_paths=" + ", ".join(str(path) for path in latest_response_paths()))
    return "; ".join(parts)


def recovery_disabled_reasons(persist: bool, temporary: bool, sync_remote: bool) -> list[str]:
    reasons: list[str] = []
    if not persist:
        reasons.append("ADVISOR_PERSIST_CONVERSATION=false")
    if temporary:
        reasons.append("ADVISOR_TEMPORARY=true")
    if not sync_remote:
        reasons.append("ADVISOR_SYNC_REMOTE=false")
    return reasons


def bool_env_default_true(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return True
    return value.lower() in ("1", "true", "yes", "on")


def call_compatible_with_recovery(
    prompt: str,
    model: str,
    timeout: int,
    *,
    _monitor_active: bool = False,
) -> str:
    old_values = {
        "ADVISOR_PERSIST_CONVERSATION": os.environ.get("ADVISOR_PERSIST_CONVERSATION"),
        "ADVISOR_TEMPORARY": os.environ.get("ADVISOR_TEMPORARY"),
        "ADVISOR_SYNC_REMOTE": os.environ.get("ADVISOR_SYNC_REMOTE"),
        "ADVISOR_AUTO_RETRY_TAIL_FRAGMENT": os.environ.get("ADVISOR_AUTO_RETRY_TAIL_FRAGMENT"),
    }
    os.environ["ADVISOR_PERSIST_CONVERSATION"] = "true"
    os.environ["ADVISOR_TEMPORARY"] = "false"
    os.environ["ADVISOR_SYNC_REMOTE"] = "true"
    os.environ["ADVISOR_AUTO_RETRY_TAIL_FRAGMENT"] = "false"
    try:
        return call_compatible(prompt, model, timeout, _monitor_active=_monitor_active)
    finally:
        for name, value in old_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def deduplicate_repeated_transport_text(text: str) -> str:
    repeated = exact_repeated_half(text)
    return repeated if repeated else text


def write_transcript(state_path: Path, conversation_data: dict[str, Any], transcript: list[dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversation_id": conversation_data.get("conversation_id"),
        "title": conversation_data.get("title"),
        "current_node": conversation_data.get("current_node") or conversation_data.get("current_node_id"),
        "messages": transcript,
    }
    safety.atomic_write_json(transcript_json_path(state_path), payload)

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
    safety.atomic_write_text(transcript_md_path(state_path), "\n".join(lines).rstrip() + "\n")


def sync_remote_conversation(
    state_path: Path,
    conversation: dict[str, Any] | None,
    timeout: int,
    project_id: str | None = None,
    expected_prompt: str | None = None,
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
        if bool_env("ADVISOR_RESET_INACCESSIBLE_CONVERSATION", True) and stale_conversation_error(exc):
            remove_state_files(state_path)
            print(
                "Advisor remote sync found an inaccessible saved conversation; "
                "cleared local advisor state so the next call starts a fresh ChatGPT chat.",
                file=sys.stderr,
            )
            return None
        print(f"Advisor remote sync skipped: {redact_sensitive(str(exc))}", file=sys.stderr)
        return conversation

    transcript = transcript_from_conversation(conversation_data)
    if expected_prompt is not None and not transcript_contains_prompt(transcript, expected_prompt):
        print(
            "Advisor remote sync did not yet contain the current prompt; keeping local adapter state.",
            file=sys.stderr,
        )
        return conversation
    latest_id = latest_finished_assistant_message_id(conversation_data)
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
    safety.atomic_write_json(state_path, payload)
    return conversation


def remote_conversation_stream_status(
    conversation_id: str,
    auth: dict[str, Any],
    timeout: int,
) -> str | None:
    url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}/stream_status"
    try:
        payload = get_json(url, auth["headers"], timeout)
    except RuntimeError:
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return str(status).strip().upper() if status is not None else None


def remote_conversation_is_streaming(status: str | None) -> bool:
    return status in {"IS_STREAMING", "STREAMING", "IN_PROGRESS", "RUNNING", "PENDING"}


def fetch_remote_final_text(
    state_path: Path,
    conversation: dict[str, Any] | None,
    prompt: str,
    timeout: int,
    project_id: str | None = None,
) -> str:
    if not conversation:
        print("Advisor final fetch skipped: no conversation id was returned by the local adapter.", file=sys.stderr)
        return ""
    conversation_id = conversation.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        print("Advisor final fetch skipped: saved conversation state has no conversation_id.", file=sys.stderr)
        return ""
    auth = load_chatgpt_auth()
    if not auth:
        print("Advisor final fetch skipped: ChatGPT HAR/auth is unavailable.", file=sys.stderr)
        return ""
    max_polls = int_env("ADVISOR_FINAL_FETCH_MAX_POLLS", 6, minimum=1)
    fallback_timeout = int_env("ADVISOR_FINAL_FETCH_TIMEOUT", max(timeout, 1), minimum=1)
    poll_seconds = float_env("ADVISOR_FINAL_FETCH_POLL_SECONDS", 5.0, minimum=0.5)
    deadline = time.monotonic() + fallback_timeout
    url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
    last_data: dict[str, Any] | None = None
    non_streaming_polls = 0
    waiting_for_stream = False
    while time.monotonic() < deadline:
        remaining_before = max(1, int(deadline - time.monotonic()))
        request_timeout = max(1, min(timeout, remaining_before))
        try:
            conversation_data = get_json(url, auth["headers"], request_timeout)
        except RuntimeError as exc:
            if os.environ.get("ADVISOR_SYNC_STRICT", "false").lower() in ("1", "true", "yes"):
                raise
            print(f"Advisor final fetch skipped: {redact_sensitive(str(exc))}", file=sys.stderr)
            return ""
        last_data = conversation_data
        text = latest_finished_assistant_text_for_prompt_data(conversation_data, prompt)
        if text:
            transcript = transcript_from_conversation(conversation_data)
            latest_id = latest_finished_assistant_message_id(conversation_data)
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
            safety.atomic_write_json(state_path, payload)
            return text
        status = remote_conversation_stream_status(conversation_id, auth, request_timeout)
        if remote_conversation_is_streaming(status):
            if not waiting_for_stream:
                print(
                    "Advisor remote ChatGPT agent turn is still running; "
                    "waiting in this process for its final response.",
                    file=sys.stderr,
                )
                waiting_for_stream = True
            non_streaming_polls = 0
        else:
            non_streaming_polls += 1
        if non_streaming_polls >= max_polls or time.monotonic() >= deadline:
            if last_data is not None:
                transcript = transcript_from_conversation(last_data)
                if transcript and transcript_contains_prompt(transcript, prompt):
                    write_transcript(state_path, last_data, transcript)
                elif transcript:
                    print(
                        "Advisor final fetch did not expose the current prompt; leaving transcript unchanged.",
                        file=sys.stderr,
                    )
            return ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        time.sleep(min(poll_seconds, remaining))
    return ""


def allow_outside_project_context() -> bool:
    return bool_env("ADVISOR_ALLOW_OUTSIDE_PROJECT_CONTEXT", False)


def compatible_api_key(base_url: str) -> str:
    explicit = os.environ.get("ADVISOR_API_KEY")
    if explicit is not None:
        return explicit
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host in {"api.openai.com"} and bool_env("ADVISOR_COMPATIBLE_USE_OPENAI_KEY", False):
        return os.environ.get("OPENAI_API_KEY", "local")
    return "local"


def build_prompt(prompt: str, context_files: list[str]) -> str:
    blocks = [prompt.strip()]
    project_dir = advisor_project_dir()
    for path in context_files:
        label, content = safety.read_context_file(
            project_dir,
            path,
            allow_outside_project=allow_outside_project_context(),
        )
        blocks.append(f"\n\n--- Context file: {label} ---\n{content}")
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


def call_compatible(
    prompt: str,
    model: str,
    timeout: int,
    *,
    _monitor_active: bool = False,
) -> str:
    if not _monitor_active:
        monitor = activity_monitor.ActivityMonitor.for_project(advisor_project_dir())
        with monitor:
            return call_compatible(prompt, model, timeout, _monitor_active=True)

    base_url = os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    api_key = compatible_api_key(base_url)
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
    use_state = persist and not temporary
    disabled = recovery_disabled_reasons(persist, temporary, sync_remote)
    if disabled:
        print(
            "Advisor transcript recovery disabled by "
            + ", ".join(disabled)
            + "; avoid these flags for normal advisor calls.",
            file=sys.stderr,
        )
    project_id = chatgpt_project_id(timeout, allow_create=use_state)
    if project_id:
        payload["gizmo_id"] = project_id
    conversation = None
    if use_state:
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
    model = resolve_compatible_model(model, base_url, headers, timeout)
    payload["model"] = model
    if is_pro_request(os.environ.get("ADVISOR_THINKING_EFFORT")) or bool_env("ADVISOR_DEBUG_ROUTE", False):
        print(
            "Advisor route: "
            f"model={model!r} thinking_effort={thinking_effort!r} "
            f"project_id={project_id or 'none'} state_path={state_path or 'none'}",
            file=sys.stderr,
        )
    try:
        response = post_json(f"{base_url}/chat/completions", payload, headers, timeout)
    except RuntimeError as exc:
        if persist and conversation and stale_conversation_error(exc):
            if state_path is not None:
                remove_state_files(state_path)
            payload.pop("conversation", None)
            response = post_json(f"{base_url}/chat/completions", payload, headers, timeout)
        else:
            raise
    synced_text = ""
    if persist and state_path is not None:
        save_conversation(state_path, response, project_id)
        if sync_remote and not temporary:
            sync_remote_conversation(state_path, load_conversation(state_path), timeout, project_id, expected_prompt=prompt)
            synced_text = latest_transcript_assistant_text_for_prompt(state_path, prompt)
    text = extract_chat_text(response)
    if (
        persist
        and sync_remote
        and not temporary
        and state_path is not None
        and not synced_text
        and transcript_has_unfinished_assistant_after_prompt(state_path, prompt)
    ):
        saved_conversation = load_conversation(state_path)
        recovered_text = fetch_remote_final_text(
            state_path,
            saved_conversation,
            prompt,
            timeout,
            project_id,
        )
        if not recovered_text:
            raise RuntimeError(
                "ChatGPT's repo-aware agent turn produced intermediate activity but did not reach "
                "a final end-of-turn response before the bounded wait expired. "
                + recovery_debug_context(state_path)
            )
        print(
            "Advisor response recovered after waiting for the remote agent turn: "
            f"local_bytes={utf8_len(text)} recovered_bytes={utf8_len(recovered_text)}",
            file=sys.stderr,
        )
        synced_text = recovered_text
    had_transport_corruption = False
    if should_prefer_synced_text(text, synced_text):
        print(
            "Advisor response recovered from synced transcript: "
            f"local_bytes={utf8_len(text)} synced_bytes={utf8_len(synced_text)}",
            file=sys.stderr,
        )
        text = synced_text
    else:
        embedded_text = ""
        embedded_conversation_data = find_conversation_data_payload(response)
        if embedded_conversation_data is not None:
            embedded_text = latest_finished_assistant_text_for_prompt_data(embedded_conversation_data, prompt)
        if should_prefer_synced_text(text, embedded_text):
            print(
                "Advisor response recovered from embedded conversation payload: "
                f"local_bytes={utf8_len(text)} embedded_bytes={utf8_len(embedded_text)}",
                file=sys.stderr,
            )
            text = embedded_text
        deduped_text = deduplicate_repeated_transport_text(text)
        if deduped_text != text:
            had_transport_corruption = True
            print(
                "Advisor response deduplicated repeated OpenAI-compatible transport text: "
                f"local_bytes={utf8_len(text)} deduped_bytes={utf8_len(deduped_text)}",
                file=sys.stderr,
            )
            text = deduped_text
    if disabled and looks_like_tail_fragment(text, prompt) and bool_env_default_true("ADVISOR_AUTO_RETRY_TAIL_FRAGMENT"):
        print(
            "Advisor response looks like a tail fragment while transcript recovery is disabled "
            f"({', '.join(disabled)}); retrying once with persistent remote sync.",
            file=sys.stderr,
        )
        return call_compatible_with_recovery(prompt, model, timeout, _monitor_active=True)
    if response_needs_remote_recovery(text, prompt, had_transport_corruption=had_transport_corruption):
        if persist and state_path is not None:
            recovered_text = fetch_remote_final_text(state_path, load_conversation(state_path), prompt, timeout, project_id)
            if recovered_text:
                print(
                    "Advisor response recovered from bounded final transcript fetch: "
                    f"local_bytes={utf8_len(text)} recovered_bytes={utf8_len(recovered_text)}",
                    file=sys.stderr,
                )
                text = recovered_text
    if response_needs_remote_recovery(text, prompt, had_transport_corruption=had_transport_corruption):
        if thinking_effort and not text.strip():
            raise RuntimeError(
                f"ChatGPT returned an empty response for thinking_effort={thinking_effort!r}. "
                "Refresh the HAR/session first; if it still fails, inspect the saved transcript "
                "and the g4f/OpenaiChat conversation-turn WebSocket handoff used by Pro/extended thinking turns. "
                + recovery_debug_context(state_path)
            )
        raise RuntimeError(
            "Advisor response still looks like a corrupted OpenAI-compatible transport fragment "
            f"after transcript recovery attempts (bytes={utf8_len(text)}). "
            "Retry with normal transcript sync enabled, or refresh the HAR/session if this repeats. "
            + recovery_debug_context(state_path)
        )
    assert_resolved_model_route(state_path, prompt)
    assert_pro_model_route(state_path, prompt)
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
        help="ChatGPT web intelligence/thinking effort, e.g. high->extended, extra-high->max, pro-extended, or none.",
    )
    parser.add_argument(
        "--provider",
        choices=["openai", "openai-compatible"],
        default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"),
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--save", help="Optional file path to write the guidance.")
    parser.add_argument("--allow-outside-project", action="store_true", help="Allow context files outside the project directory.")
    activity = parser.add_mutually_exclusive_group()
    activity.add_argument(
        "--live-activity",
        dest="live_activity",
        action="store_true",
        help="Show safe local DevSpace tool activity while a foreground advisor call runs (default).",
    )
    activity.add_argument(
        "--no-live-activity",
        dest="live_activity",
        action="store_false",
        help="Disable local DevSpace activity and heartbeat lines.",
    )
    parser.set_defaults(live_activity=None)
    return parser.parse_args()


def coordinated_state_path(timeout: int) -> Path | None:
    persist = os.environ.get("ADVISOR_PERSIST_CONVERSATION", "true").lower() in ("1", "true", "yes")
    temporary = os.environ.get("ADVISOR_TEMPORARY", "false").lower() in ("1", "true", "yes")
    if not persist or temporary:
        return None
    with concurrency.project_binding_lock(advisor_project_dir()):
        chatgpt_project_id(timeout, allow_create=True)
        return default_state_path()


def write_guidance_outputs(args: argparse.Namespace, guidance: str) -> list[Path]:
    if args.save:
        safety.atomic_write_text(Path(args.save), guidance)
    return [path for path in latest_response_paths() if write_latest_response(path, guidance)]


def main() -> int:
    configure_stdio()
    args = parse_args()
    if args.thinking_effort is not None:
        os.environ["ADVISOR_THINKING_EFFORT"] = args.thinking_effort
    if args.allow_outside_project:
        os.environ["ADVISOR_ALLOW_OUTSIDE_PROJECT_CONTEXT"] = "true"
    if args.live_activity is not None:
        os.environ["ADVISOR_LIVE_ACTIVITY"] = "true" if args.live_activity else "false"
    args.thinking_effort = select_request_thinking_effort(args.thinking_effort)
    if args.thinking_effort is None:
        os.environ.pop("ADVISOR_THINKING_EFFORT", None)
    else:
        os.environ["ADVISOR_THINKING_EFFORT"] = args.thinking_effort
    args.model = select_request_model(args.thinking_effort, args.model)
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    prompt = sanitize_text(build_prompt(prompt, args.context_file))
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2

    if args.provider == "openai":
        guidance = call_openai(prompt, args.model, args.timeout)
        written_latest_paths = write_guidance_outputs(args, guidance)
    else:
        configured_base_url = os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
        state_path = coordinated_state_path(args.timeout)
        with concurrency.coordinated_call(
            configured_base_url,
            state_path,
            request_timeout=args.timeout,
        ) as lease:
            previous_base_url = os.environ.get("ADVISOR_BASE_URL")
            os.environ["ADVISOR_BASE_URL"] = lease.url
            try:
                guidance = call_compatible(prompt, args.model, args.timeout)
                written_latest_paths = write_guidance_outputs(args, guidance)
            except BaseException as exc:
                if concurrency.transport_failure(exc):
                    lease.report_failure()
                raise
            else:
                lease.report_success()
            finally:
                if previous_base_url is None:
                    os.environ.pop("ADVISOR_BASE_URL", None)
                else:
                    os.environ["ADVISOR_BASE_URL"] = previous_base_url

    if written_latest_paths:
        print(
            "Advisor latest-response saved: " + ", ".join(str(path) for path in written_latest_paths),
            file=sys.stderr,
        )
    print(guidance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
