#!/usr/bin/env python3
"""Shared safety helpers for advisor scripts."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any


SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    "auth_openaichat.json",
    "conversation.json",
    "transcript.json",
    "transcript.md",
}

SENSITIVE_SUFFIXES = (
    ".har",
    ".cookie.json",
    ".cookies.json",
    ".pem",
    ".key",
)

PROMPT_ARG_NAMES = {
    "--prompt",
    "--draft",
    "--error-output",
    "--failure",
    "--accepted-advice",
    "--rejected-advice",
    "--outcome",
    "--notes",
    "--lesson",
    "--question",
}


def sanitize_text(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    return str(text).encode("utf-8", errors="replace").decode("utf-8")


def prompt_protection_enabled() -> bool:
    """Return whether prompt-only transport should apply legacy redaction."""
    return os.environ.get("ADVISOR_PROMPT_PROTECTION", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def prepare_prompt_text(text: str | bytes | None) -> str:
    """Prepare deliberate prompt-only input without changing it by default."""
    value = sanitize_text(text)
    return redact_sensitive_text(value) if prompt_protection_enabled() else value


def truncate(text: str | bytes | None, limit: int) -> str:
    value = sanitize_text(text)
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"


def redact_sensitive_text(text: str | bytes | None) -> str:
    value = sanitize_text(text)
    patterns = [
        (r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", "[REDACTED_JWT]"),
        (r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}", "Bearer [REDACTED]"),
        (r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(access[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(refresh[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(session[_-]?id['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(cookie['\"]?\s*[:=]\s*['\"]?)[^'\"}]+", r"\1[REDACTED]"),
        (r"(?i)(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(secret['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(password['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"sk-[A-Za-z0-9_-]{20,}", "sk-[REDACTED]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]"),
    ]
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value


def safe_slug(value: str, default: str = "item", max_length: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    if not slug or slug in {".", ".."}:
        slug = default
    return slug[:max_length]


def safe_key_slug(value: str, default: str = "conversation", max_slug_length: int = 56) -> str:
    slug = safe_slug(value, default=default, max_length=max_slug_length)
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{slug}-{digest}"


def is_sensitive_path(project_dir: Path, path: Path) -> bool:
    resolved_project = project_dir.resolve()
    resolved_path = path.resolve()
    name = resolved_path.name.lower()
    try:
        relative_parts = [part.lower() for part in resolved_path.relative_to(resolved_project).parts]
    except ValueError:
        relative_parts = [part.lower() for part in resolved_path.parts]
    if (
        len(relative_parts) >= 3
        and relative_parts[0] == ".codex-advisor"
        and relative_parts[1] == "context-packs"
        and name.endswith("-context-pack.md")
    ):
        return False
    if ".codex-advisor" in relative_parts:
        return True
    if "har_and_cookies" in relative_parts:
        return True
    if name in SENSITIVE_FILE_NAMES:
        return True
    if name.startswith(".env."):
        return True
    if name.startswith("auth_") and name.endswith(".json"):
        return True
    return any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def resolve_input_file(
    project_dir: Path,
    raw: str,
    allow_outside_project: bool = False,
    *,
    allow_sensitive: bool = False,
) -> Path:
    raw_path = Path(raw)
    path = raw_path.resolve() if raw_path.is_absolute() else (project_dir / raw_path).resolve()
    try:
        path.relative_to(project_dir.resolve())
        in_project = True
    except ValueError:
        in_project = False
    if not in_project and not allow_outside_project:
        raise RuntimeError(f"Refusing to include file outside the project: {path}")
    if not allow_sensitive and is_sensitive_path(project_dir, path):
        raise RuntimeError(f"Refusing to include advisor state, HAR/cookie/auth, env, or key material: {path}")
    if not path.exists():
        raise RuntimeError(f"Context file does not exist: {path}")
    if not path.is_file():
        raise RuntimeError(f"Context path is not a file: {path}")
    return path


def read_limited_text(path: Path, limit: int | None = None, redact: bool = True) -> str:
    text = sanitize_text(path.read_text(encoding="utf-8", errors="replace"))
    if redact:
        text = redact_sensitive_text(text)
    return truncate(text, limit) if limit is not None else text


def read_context_file(
    project_dir: Path,
    raw: str,
    limit: int | None = None,
    allow_outside_project: bool = False,
    *,
    redact: bool = True,
    allow_sensitive: bool = False,
) -> tuple[str, str]:
    path = resolve_input_file(
        project_dir,
        raw,
        allow_outside_project,
        allow_sensitive=allow_sensitive,
    )
    try:
        label = str(path.relative_to(project_dir.resolve()))
    except ValueError:
        label = str(path)
    return label, read_limited_text(path, limit, redact=redact)


def read_prompt_context_file(
    project_dir: Path,
    raw: str,
    limit: int | None = None,
    allow_outside_project: bool = False,
) -> tuple[str, str]:
    """Read an explicitly selected prompt-only context file verbatim by default."""
    protect = prompt_protection_enabled()
    return read_context_file(
        project_dir,
        raw,
        limit,
        allow_outside_project=allow_outside_project or not protect,
        redact=protect,
        allow_sensitive=not protect,
    )


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, sanitize_text(text).encode("utf-8"), mode)


def atomic_write_json(path: Path, data: Any, mode: int = 0o600, *, sort_keys: bool = False) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=sort_keys), mode)


def redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if arg in PROMPT_ARG_NAMES or arg.endswith("-file") or arg in {"--context-file"}:
            redacted.append(arg)
            redact_next = True
            continue
        if any(arg.startswith(name + "=") for name in PROMPT_ARG_NAMES):
            name = arg.split("=", 1)[0]
            redacted.append(f"{name}=[REDACTED]")
            continue
        redacted.append(redact_sensitive_text(arg))
    return redacted
