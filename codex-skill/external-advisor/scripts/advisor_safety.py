#!/usr/bin/env python3
"""Shared safety helpers for advisor scripts."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any


SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    "advisor-tunnel-id",
    "allowed-email",
    "auth_openaichat.json",
    "cf-access-client-id",
    "cf-access-client-secret",
    "cloudflare-api-token",
    "cloudflare-audit-token",
    "cloudflared-token",
    "config.pre-runtime-migration.json",
    "conversation.json",
    "gateway-runtime.json",
    "install-cloudflared-token.sh",
    "oauth-token.json",
    "oauth_tokens.json",
    "origin-secret",
    "pinned-root",
    "terraform.tfstate",
    "token.json",
    "tokens.json",
    "transcript.json",
    "transcript.md",
    "tunnel-config.pre-runtime-migration.json",
    "tunnel-token",
}

SENSITIVE_SUFFIXES = (
    ".7z",
    ".agekey",
    ".db",
    ".duckdb",
    ".gz",
    ".h5",
    ".har",
    ".ipynb",
    ".joblib",
    ".cookie.json",
    ".cookies.json",
    ".doc",
    ".docx",
    ".jks",
    ".kdbx",
    ".keystore",
    ".npy",
    ".npz",
    ".onnx",
    ".parquet",
    ".pcap",
    ".pcapng",
    ".pem",
    ".key",
    ".p12",
    ".pdf",
    ".pfx",
    ".pickle",
    ".pkl",
    ".ppt",
    ".pptx",
    ".pt",
    ".pth",
    ".rar",
    ".safetensors",
    ".saz",
    ".secret",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tfstate",
    ".tgz",
    ".token",
    ".xls",
    ".xlsx",
    ".zip",
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

HARDENED_GIT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
HARDENED_GIT_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "diff.external=",
    "interactive.diffFilter=",
    "core.attributesFile=/dev/null",
    "core.excludesFile=/dev/null",
    "credential.helper=",
    "core.askPass=/bin/false",
    "core.sshCommand=/bin/false",
    "pager.status=false",
    "color.ui=false",
    "protocol.allow=never",
)
UNSAFE_GIT_CONFIG_KEY_RE = re.compile(
    r"(?im)^\s*(?:"
    r"fsmonitor|hookspath|worktree|worktreeconfig|sshcommand|"
    r"attributesfile|excludesfile|askpass|helper|command|textconv|"
    r"difffilter|clean|smudge|process|driver"
    r")\s*="
)


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


def trusted_executable(name: str, *, project_dir: Path | None = None) -> Path:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"Required executable not found: {name}")
    path = Path(resolved).resolve(strict=True)
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or (hasattr(os, "getuid") and metadata.st_uid not in {0, os.getuid()})
    ):
        raise RuntimeError(
            f"{name} must be a root- or user-owned regular executable that is not "
            "group/world writable."
        )
    if project_dir is not None:
        try:
            path.relative_to(project_dir.resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError(f"Refusing to use {name} from inside the repository.")
    return path


def hardened_git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "/bin/false",
    }


def verify_repository_git_config_safe(project_dir: Path) -> None:
    project = project_dir.resolve(strict=True)
    git_dir = project / ".git"
    try:
        git_metadata = git_dir.lstat()
    except OSError as exc:
        raise RuntimeError("Repository Git metadata is unavailable.") from exc
    if (
        git_dir.is_symlink()
        or not stat.S_ISDIR(git_metadata.st_mode)
        or git_metadata.st_dev != project.lstat().st_dev
    ):
        raise RuntimeError(
            "Hardened repository inspection requires a real in-tree .git directory."
        )
    config = git_dir / "config"
    try:
        metadata = config.lstat()
    except OSError as exc:
        raise RuntimeError("Repository Git config is unavailable.") from exc
    if (
        config.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink > 1
        or metadata.st_size > 1024 * 1024
        or metadata.st_dev != git_metadata.st_dev
    ):
        raise RuntimeError("Repository Git config is unsafe.")
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Repository Git config could not be inspected safely.") from exc
    if re.search(r"(?im)^\s*\[\s*include(?:if)?\b", text):
        raise RuntimeError("Git config include directives are not permitted.")
    if UNSAFE_GIT_CONFIG_KEY_RE.search(text):
        raise RuntimeError("Command-bearing or external-path Git config is not permitted.")
    alternates = git_dir / "objects" / "info" / "alternates"
    try:
        if alternates.exists() and alternates.read_bytes().strip():
            raise RuntimeError("Git alternate object databases are not permitted.")
    except OSError as exc:
        raise RuntimeError("Git alternate-object configuration is unsafe.") from exc


def run_hardened_git(
    project_dir: Path,
    arguments: list[str] | tuple[str, ...],
    *,
    text: bool = False,
    timeout: int = 30,
    maximum_output_bytes: int = HARDENED_GIT_MAX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[Any]:
    project = project_dir.resolve(strict=True)
    verify_repository_git_config_safe(project)
    git = trusted_executable("git", project_dir=project)
    command = [str(git), "--no-optional-locks"]
    for entry in HARDENED_GIT_CONFIG:
        command.extend(["-c", entry])
    command.extend(["-C", str(project), *arguments])
    completed = subprocess.run(
        command,
        env=hardened_git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        timeout=timeout,
        check=False,
    )
    stdout_size = (
        len(completed.stdout.encode("utf-8", errors="replace"))
        if isinstance(completed.stdout, str)
        else len(completed.stdout or b"")
    )
    stderr_size = (
        len(completed.stderr.encode("utf-8", errors="replace"))
        if isinstance(completed.stderr, str)
        else len(completed.stderr or b"")
    )
    if stdout_size + stderr_size > maximum_output_bytes:
        raise RuntimeError("Git command output exceeded its safety limit.")
    return completed


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
