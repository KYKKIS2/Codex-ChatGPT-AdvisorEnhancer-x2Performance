#!/usr/bin/env python3
"""Safe repo-aware advisor agent-mode helpers.

This script prepares a review-first handoff for ChatGPT through a
DevSpace-compatible MCP bridge. It never starts DevSpace, opens a tunnel,
invokes npx packages, contacts ChatGPT, writes credentials, or grants edit
authority by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import advisor_concurrency as concurrency
import advisor_safety as safety


AGENT_MODE_ENV = "ADVISOR_AGENT_MODE"
ALLOWED_ROOTS_ENV = "ADVISOR_AGENT_ALLOWED_ROOTS"
BRIDGE_EXECUTABLE_ENV = "ADVISOR_AGENT_BRIDGE_EXECUTABLE"
ALLOW_PROJECT_BRIDGE_ENV = "ADVISOR_AGENT_ALLOW_PROJECT_BRIDGE"
REQUIRE_BRIDGE_ENV = "ADVISOR_AGENT_REQUIRE_BRIDGE"
CASE_INSENSITIVE_ENV = "ADVISOR_AGENT_CASE_INSENSITIVE_PATHS"
CONFIG_PATH_ENV = "ADVISOR_AGENT_CONFIG"
ALLOW_SENSITIVE_PROJECT_ENV = "ADVISOR_AGENT_ALLOW_SENSITIVE_PROJECT"
SECRET_SCAN_ENV = "ADVISOR_AGENT_SECRET_SCAN"
SECRET_SCAN_MAX_FILES_ENV = "ADVISOR_AGENT_SECRET_SCAN_MAX_FILES"
SECRET_SCAN_MAX_BYTES_ENV = "ADVISOR_AGENT_SECRET_SCAN_MAX_BYTES"
SANITIZED_SOURCE_MAX_BYTES_ENV = "ADVISOR_AGENT_SANITIZED_SOURCE_MAX_BYTES"
SANITIZED_MAX_ENTRIES_ENV = "ADVISOR_AGENT_SANITIZED_MAX_ENTRIES"
SANITIZED_MAX_TOTAL_BYTES_ENV = "ADVISOR_AGENT_SANITIZED_MAX_TOTAL_BYTES"
SANITIZED_WORKSPACE_ENV = "ADVISOR_AGENT_SANITIZED_WORKSPACE"
WORKSPACE_ROOT_ENV = "ADVISOR_AGENT_WORKSPACE_ROOT"

DEFAULT_BRIDGE_EXECUTABLE = "devspace"
DEFAULT_AGENT_MODE = "auto"
VALID_AGENT_MODES = {"auto", "on", "off"}
DEFAULT_SANITIZED_WORKSPACE_MODE = "always"
VALID_SANITIZED_WORKSPACE_MODES = {"auto", "always", "off"}
CONFIG_SCHEMA_VERSION = "1.0"
DEFAULT_SECRET_SCAN_MAX_FILES = 20000
DEFAULT_SECRET_SCAN_MAX_BYTES = 262144
DEFAULT_SANITIZED_SOURCE_MAX_BYTES = 1048576
DEFAULT_SANITIZED_MAX_ENTRIES = 20000
DEFAULT_SANITIZED_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_SOURCE_TREE_MAX_ENTRIES = 500000
SANITIZED_LARGE_SOURCE_SUFFIXES = frozenset({".py", ".pyi"})

SENSITIVE_AGENT_DIR_NAMES = {
    ".aws",
    ".azure",
    ".codex-advisor",
    ".config/gcloud",
    ".gnupg",
    ".kube",
    ".mozilla",
    ".ssh",
    "brave-browser",
    "chromium",
    "cookies",
    "firefox",
    "google-chrome",
    "har_and_cookies",
    "keystore",
    "microsoft-edge",
    "wallets",
}

SECRET_SCAN_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "vendor",
}

SANITIZED_SKIP_FILE_SUFFIXES = (
    ".7z",
    ".db",
    ".duckdb",
    ".gz",
    ".h5",
    ".joblib",
    ".onnx",
    ".parquet",
    ".pickle",
    ".pkl",
    ".pt",
    ".pth",
    ".rar",
    ".safetensors",
    ".sqlite",
    ".tar",
    ".tgz",
    ".zip",
)

SANITIZED_GENERATED_FILE_NAMES = {
    "ADVISOR_SANITIZED_WORKSPACE.md",
    "SANITIZED_WORKSPACE_MANIFEST.json",
}

BROWSER_DIR_MARKERS = {
    "application support/google/chrome",
    "application support/firefox",
    "application support/brave software",
    "application support/microsoft edge",
    "appdata/local/google/chrome",
    "appdata/local/microsoft/edge",
    "appdata/roaming/mozilla/firefox",
    ".mozilla/firefox",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
}

SENSITIVE_AGENT_NAME_MARKERS = (
    "auth_openaichat",
    "cookie",
    "mnemonic",
    "private-key",
    "private_key",
    "seed-phrase",
    "seed_phrase",
    "wallet",
)

SENSITIVE_AGENT_FILE_NAMES = {
    ".dockerconfigjson",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "pip.conf",
    "terraform.tfstate",
}

SENSITIVE_AGENT_SUFFIXES = (
    *safety.SENSITIVE_SUFFIXES,
    ".jks",
    ".keystore",
    ".kdbx",
    ".p12",
    ".pfx",
)

FULL_ACCESS_SENSITIVE_NAME_MARKERS = (
    "auth_openaichat",
    "mnemonic",
    "private-key",
    "private_key",
    "seed-phrase",
    "seed_phrase",
)

FULL_ACCESS_SENSITIVE_SUFFIXES = (
    ".agekey",
    ".cookie.json",
    ".cookies.json",
    ".har",
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
    ".token",
)

CONTENT_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "private key material"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"), "JWT-like token"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "OpenAI-style API key"),
    (
        re.compile(
            r"(?i)['\"]?\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|private[_-]?key)\b['\"]?"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{16,}"
        ),
        "secret-looking assignment",
    ),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"), "GitHub token"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "AWS access key id"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), "Slack token"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b"), "Stripe secret key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "Anthropic API key"),
    (re.compile(r"\b(?:npm|hf)_[A-Za-z0-9]{24,}\b"), "package or model registry token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "GitLab access token"),
    (re.compile(r"\bdop_v1_[A-Fa-f0-9]{32,}\b"), "DigitalOcean token"),
)


@dataclass
class ValidationResult:
    ok: bool
    path: str
    resolved_path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class BridgeStatus:
    ok: bool
    executable: str
    resolved_path: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class WorktreeStatus:
    available: bool
    inside_git_repo: bool
    has_head_commit: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class SecretFinding:
    path: str
    kind: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "reason": self.reason}


@dataclass
class SecretScanResult:
    ok: bool
    project_dir: str
    scanned_files: int = 0
    scanned_dirs: int = 0
    skipped_dirs: int = 0
    scanned_content_files: int = 0
    findings: list[SecretFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    allow_sensitive_project: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_dir": self.project_dir,
            "scanned_files": self.scanned_files,
            "scanned_dirs": self.scanned_dirs,
            "skipped_dirs": self.skipped_dirs,
            "scanned_content_files": self.scanned_content_files,
            "findings": [finding.to_dict() for finding in self.findings],
            "errors": self.errors,
            "warnings": self.warnings,
            "truncated": self.truncated,
            "allow_sensitive_project": self.allow_sensitive_project,
        }


@dataclass
class SanitizedWorkspaceStatus:
    mode: str
    used: bool
    source_dir: str
    workspace_dir: str = ""
    workspace_root: str = ""
    workspace_allowed_root: str = ""
    private_manifest_path: str = ""
    generation_id: str = ""
    source_fingerprint: str = ""
    reused: bool = False
    copied_files: int = 0
    copied_dirs: int = 0
    redacted_files: int = 0
    skipped_files: int = 0
    skipped_dirs: int = 0
    skipped_symlinks: int = 0
    reason: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "used": self.used,
            "source_dir": self.source_dir,
            "workspace_dir": self.workspace_dir,
            "workspace_root": self.workspace_root,
            "workspace_allowed_root": self.workspace_allowed_root,
            "private_manifest_path": self.private_manifest_path,
            "generation_id": self.generation_id,
            "source_fingerprint": self.source_fingerprint,
            "reused": self.reused,
            "copied_files": self.copied_files,
            "copied_dirs": self.copied_dirs,
            "redacted_files": self.redacted_files,
            "skipped_files": self.skipped_files,
            "skipped_dirs": self.skipped_dirs,
            "skipped_symlinks": self.skipped_symlinks,
            "reason": self.reason,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class SanitizedCopyEntry:
    source: Path
    relative: Path
    size: int
    mtime_ns: int
    mode: int
    source_sha256: str
    target_sha256: str
    redacted_content: bytes | None = None
    redaction_reason: str = ""


@dataclass
class SanitizedCopyPlan:
    entries: list[SanitizedCopyEntry] = field(default_factory=list)
    skipped_files: int = 0
    skipped_dirs: int = 0
    skipped_symlinks: int = 0
    skipped_samples: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    redacted_paths: list[str] = field(default_factory=list)
    fingerprint: str = ""
    copied_bytes: int = 0
    scanned_entries: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class AgentModeStatus:
    requested_mode: str
    available: bool
    project_dir: str
    allowed_roots: list[str]
    selected_root: str = ""
    bridge: BridgeStatus | None = None
    worktree: WorktreeStatus | None = None
    secret_scan: SecretScanResult | None = None
    sanitized_workspace: SanitizedWorkspaceStatus | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "available": self.available,
            "project_dir": self.project_dir,
            "allowed_roots": self.allowed_roots,
            "selected_root": self.selected_root,
            "bridge": self.bridge.__dict__ if self.bridge else None,
            "worktree": self.worktree.__dict__ if self.worktree else None,
            "secret_scan": self.secret_scan.to_dict() if self.secret_scan else None,
            "sanitized_workspace": self.sanitized_workspace.to_dict() if self.sanitized_workspace else None,
            "errors": self.errors,
            "warnings": self.warnings,
            "notes": self.notes,
        }


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def falsey(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def split_roots(*values: str | None) -> list[str]:
    roots: list[str] = []
    for value in values:
        if not value:
            continue
        for raw in value.split(","):
            item = raw.strip()
            if item:
                roots.append(item)
    return roots


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def default_config_path() -> Path:
    return codex_home() / "advisor-agent" / "config.json"


def default_workspace_root() -> Path:
    return codex_home() / "advisor-agent" / "workspaces"


def workspace_root(raw: str | Path | None = None) -> Path:
    configured = raw or os.environ.get(WORKSPACE_ROOT_ENV)
    return Path(configured).expanduser().resolve() if configured else default_workspace_root()


def config_path(raw: str | Path | None = None) -> Path:
    configured = raw or os.environ.get(CONFIG_PATH_ENV)
    return Path(configured).expanduser().resolve() if configured else default_config_path()


def load_agent_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = config_path(path)
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_errors": [f"could not read agent config {cfg_path}: {exc}"]}
    return data if isinstance(data, dict) else {"_errors": [f"agent config must be a JSON object: {cfg_path}"]}


def config_allowed_roots(path: str | Path | None = None) -> list[str]:
    data = load_agent_config(path)
    roots = data.get("allowed_roots", [])
    if not isinstance(roots, list):
        return []
    return [str(root) for root in roots if str(root).strip()]


def write_agent_config_roots(roots: list[str], *, path: str | Path | None = None) -> Path:
    cfg_path = config_path(path)
    existing = load_agent_config(cfg_path)
    payload = {key: value for key, value in existing.items() if not key.startswith("_")}
    now = datetime.now(timezone.utc).isoformat()
    created = payload.get("created_utc") if isinstance(payload.get("created_utc"), str) else now
    payload.update(
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "created_utc": created,
            "updated_utc": now,
            "allowed_roots": roots,
        }
    )
    safety.atomic_write_json(cfg_path, payload)
    return cfg_path


def merge_roots(existing: list[str], additions: list[str], *, case_insensitive: bool = False) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for raw in [*existing, *additions]:
        if not str(raw).strip():
            continue
        try:
            resolved = str(resolve_path(raw))
        except OSError:
            resolved = str(Path(raw).expanduser())
        key = resolved.casefold() if case_insensitive else resolved
        if key not in seen:
            seen.add(key)
            merged.append(resolved)
    return merged


def default_case_insensitive() -> bool:
    if os.environ.get(CASE_INSENSITIVE_ENV):
        return truthy(os.environ.get(CASE_INSENSITIVE_ENV))
    return os.name == "nt" or platform.system().lower() == "darwin"


def resolve_path(raw: str | Path) -> Path:
    return Path(raw).expanduser().resolve()


def path_key(path: Path, *, case_insensitive: bool) -> str:
    text = str(path)
    return text.casefold() if case_insensitive else text


def path_is_same_or_child(path: Path, parent: Path, *, case_insensitive: bool = False) -> bool:
    if case_insensitive:
        child = path_key(path, case_insensitive=True).rstrip("/\\")
        root = path_key(parent, case_insensitive=True).rstrip("/\\")
        return child == root or child.startswith(root + os.sep) or child.startswith(root + "/") or child.startswith(root + "\\")
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_drive_or_filesystem_root(path: Path) -> bool:
    resolved = path.resolve()
    if resolved.parent == resolved:
        return True
    anchor = Path(resolved.anchor) if resolved.anchor else None
    return bool(anchor and resolved == anchor)


def relative_parts(path: Path) -> list[str]:
    parts = [part for part in path.parts if part not in {path.anchor, os.sep, ""}]
    return [part.lower() for part in parts]


def contains_sensitive_agent_marker(path: Path) -> bool:
    parts = relative_parts(path)
    joined = "/".join(parts)
    name = path.name.lower()
    if any(marker.lower() in joined for marker in BROWSER_DIR_MARKERS):
        return True
    if any(part in SENSITIVE_AGENT_DIR_NAMES for part in parts):
        return True
    if any(marker in name for marker in SENSITIVE_AGENT_NAME_MARKERS):
        return True
    if name in safety.SENSITIVE_FILE_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in safety.SENSITIVE_SUFFIXES):
        return True
    return False


def sensitive_reason(path: Path) -> str:
    parts = relative_parts(path)
    joined = "/".join(parts)
    if ".codex-advisor" in parts:
        return "advisor state directories are denied"
    if "har_and_cookies" in parts or path.name.lower().endswith(".har"):
        return "HAR/cookie authentication material is denied"
    if ".env" == path.name.lower() or path.name.lower().startswith(".env."):
        return "environment files are denied"
    if ".ssh" in parts:
        return "SSH key directories are denied"
    if any(marker.lower() in joined for marker in BROWSER_DIR_MARKERS):
        return "browser profile directories are denied"
    if "wallets" in parts or "wallet" in path.name.lower() or "private-key" in path.name.lower() or "private_key" in path.name.lower():
        return "wallet/private-key paths are denied"
    return "sensitive local state is denied"


def safe_relative_label(project: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project.resolve()))
    except (OSError, ValueError):
        try:
            return str(path.relative_to(project))
        except ValueError:
            return path.name or str(path)


def project_relative_parts(project: Path, path: Path) -> list[str]:
    try:
        rel = path.resolve().relative_to(project.resolve())
        return [part.lower() for part in rel.parts if part not in {"", os.sep}]
    except (OSError, ValueError):
        return relative_parts(path)


def contains_sensitive_project_relative_marker(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return True
    parts = [part.lower() for part in relative.parts if part not in {"", os.sep}]
    joined = "/".join(parts)
    name = relative.name.lower()
    if ".codex-advisor" in parts:
        return True
    if any(marker.lower() in joined for marker in BROWSER_DIR_MARKERS):
        return True
    if any(part in SENSITIVE_AGENT_DIR_NAMES for part in parts):
        return True
    if name in SENSITIVE_AGENT_FILE_NAMES:
        return True
    if name in safety.SENSITIVE_FILE_NAMES or name.startswith(".env."):
        return True
    if name.startswith("auth_") and name.endswith(".json"):
        return True
    if any(marker in name for marker in SENSITIVE_AGENT_NAME_MARKERS):
        return True
    if any(name.endswith(suffix) for suffix in SENSITIVE_AGENT_SUFFIXES):
        return True
    return False


def contains_sensitive_full_access_relative_marker(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return True
    parts = [part.lower() for part in relative.parts if part not in {"", os.sep}]
    joined = "/".join(parts)
    name = relative.name.lower()
    if ".codex-advisor" in parts:
        return True
    if any(marker.lower() in joined for marker in BROWSER_DIR_MARKERS):
        return True
    if any(part in SENSITIVE_AGENT_DIR_NAMES for part in parts):
        return True
    if name in SENSITIVE_AGENT_FILE_NAMES or name in safety.SENSITIVE_FILE_NAMES:
        return True
    if name.startswith(".env.") or (
        name.startswith("auth_") and name.endswith(".json")
    ):
        return True
    if any(marker in name for marker in FULL_ACCESS_SENSITIVE_NAME_MARKERS):
        return True
    return any(name.endswith(suffix) for suffix in FULL_ACCESS_SENSITIVE_SUFFIXES)


def contains_sensitive_project_marker(project: Path, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(project.resolve())
    except (OSError, ValueError):
        return contains_sensitive_agent_marker(path)
    return contains_sensitive_project_relative_marker(relative)


def project_sensitive_reason(project: Path, path: Path) -> str:
    parts = project_relative_parts(project, path)
    joined = "/".join(parts)
    name = path.name.lower()
    if ".codex-advisor" in parts:
        return "advisor transcripts/conversation state are denied"
    if "har_and_cookies" in parts or name.endswith(".har"):
        return "HAR/cookie authentication material is denied"
    if name == ".env" or name.startswith(".env."):
        return "environment files are denied"
    if ".ssh" in parts or name in SENSITIVE_AGENT_FILE_NAMES:
        return "SSH/private key files are denied"
    if any(marker.lower() in joined for marker in BROWSER_DIR_MARKERS):
        return "browser profile directories are denied"
    if (
        "wallets" in parts
        or "wallet" in name
        or "mnemonic" in name
        or "seed" in name
        or "private-key" in name
        or "private_key" in name
    ):
        return "wallet/seed/private-key paths are denied"
    if name.startswith("auth_") and name.endswith(".json"):
        return "local auth JSON files are denied"
    if any(name.endswith(suffix) for suffix in SENSITIVE_AGENT_SUFFIXES):
        return "key/certificate database material is denied"
    return "sensitive local state is denied"


def file_looks_binary(sample: bytes) -> bool:
    return b"\0" in sample


def content_secret_reason(path: Path, *, max_bytes: int) -> str:
    try:
        _stat_result, sample = read_regular_file(path, max_content_bytes=max_bytes)
    except (OSError, RuntimeError) as exc:
        return f"could not inspect file content: {exc}"
    if file_looks_binary(sample):
        return ""
    text = sample.decode("utf-8", errors="ignore")
    for pattern, reason in CONTENT_SECRET_PATTERNS:
        if pattern.search(text):
            return reason
    return ""


def content_secret_reason_text(text: str) -> str:
    for pattern, reason in CONTENT_SECRET_PATTERNS:
        if pattern.search(text):
            return reason
    return ""


PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_sanitized_text(text: str) -> str:
    value = PRIVATE_KEY_BLOCK_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
    replacements = (
        (CONTENT_SECRET_PATTERNS[1][0], "[REDACTED_JWT]"),
        (CONTENT_SECRET_PATTERNS[2][0], "sk-[REDACTED]"),
        (CONTENT_SECRET_PATTERNS[3][0], "[REDACTED_SECRET_ASSIGNMENT]"),
    )
    for pattern, replacement in replacements:
        value = pattern.sub(replacement, value)
    for pattern, _reason in CONTENT_SECRET_PATTERNS[4:]:
        value = pattern.sub("[REDACTED_PROVIDER_TOKEN]", value)
    return safety.redact_sensitive_text(value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("hash target is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def decode_mountinfo_path(value: str) -> str:
    try:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )
    except ValueError as exc:
        raise RuntimeError("the kernel mount table contained an invalid path escape") from exc


def source_descendant_mount_points(
    project: Path,
    *,
    mountinfo_text: str | None = None,
) -> list[Path]:
    project = project.resolve(strict=True)
    if mountinfo_text is None:
        mountinfo = Path("/proc/self/mountinfo")
        if not mountinfo.is_file():
            return []
        raw = mountinfo.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("the process mount table exceeded its safety limit")
        mountinfo_text = raw.decode("utf-8", errors="surrogateescape")
    descendants: set[Path] = set()
    for line in mountinfo_text.splitlines():
        fields = line.split(" ")
        if len(fields) < 6 or " - " not in line:
            raise RuntimeError("the process mount table was malformed")
        mount_point = Path(os.path.normpath(decode_mountinfo_path(fields[4])))
        if not mount_point.is_absolute():
            raise RuntimeError("the process mount table contained a relative mount point")
        try:
            relative = mount_point.relative_to(project)
        except ValueError:
            continue
        if relative != Path("."):
            descendants.add(mount_point)
    return sorted(descendants, key=str)


def verify_source_tree_boundary(
    project_dir: str | Path,
    *,
    mountinfo_text: str | None = None,
    max_entries: int = DEFAULT_SOURCE_TREE_MAX_ENTRIES,
) -> None:
    project = resolve_path(project_dir)
    root_metadata = project.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("source project root is not a directory")
    mounts = source_descendant_mount_points(project, mountinfo_text=mountinfo_text)
    if mounts:
        relative = mounts[0].relative_to(project)
        raise RuntimeError(f"source project contains a descendant mount point at {relative}")

    errors: list[OSError] = []
    entries = 0
    for root, directories, files in os.walk(
        project,
        topdown=True,
        followlinks=False,
        onerror=errors.append,
    ):
        root_path = Path(root)
        for name in [*directories, *files]:
            entries += 1
            if entries > max_entries:
                raise RuntimeError("source project exceeded the boundary-scan entry limit")
            path = root_path / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise RuntimeError("source project changed during its boundary scan") from exc
            if metadata.st_dev != root_metadata.st_dev:
                raise RuntimeError("source project crosses a filesystem device boundary")
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                raise RuntimeError("source project contains a hardlinked regular file")
            if not (
                stat.S_ISREG(metadata.st_mode)
                or stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
            ):
                raise RuntimeError(
                    "source project contains a socket, device, FIFO, or other unsupported entry"
                )
    if errors:
        raise RuntimeError("source project could not be scanned completely")


def scan_project_secrets(
    project_dir: str | Path,
    *,
    allow_sensitive_project: bool = False,
    max_files: int | None = None,
    max_content_bytes: int | None = None,
) -> SecretScanResult:
    project = resolve_path(project_dir)
    max_files = max_files if max_files is not None else int(os.environ.get(SECRET_SCAN_MAX_FILES_ENV, DEFAULT_SECRET_SCAN_MAX_FILES))
    max_content_bytes = (
        max_content_bytes
        if max_content_bytes is not None
        else int(os.environ.get(SECRET_SCAN_MAX_BYTES_ENV, DEFAULT_SECRET_SCAN_MAX_BYTES))
    )
    result = SecretScanResult(False, str(project), allow_sensitive_project=allow_sensitive_project)

    if not project.exists() or not project.is_dir():
        result.errors.append("project directory does not exist or is not a directory")
        return result
    try:
        verify_source_tree_boundary(project)
    except (OSError, RuntimeError) as exc:
        result.errors.append(f"source boundary validation failed: {exc}")
        return result

    def add_finding(path: Path, kind: str, reason: str) -> None:
        result.findings.append(SecretFinding(safe_relative_label(project, path), kind, reason))

    def onerror(error: OSError) -> None:
        result.errors.append(f"could not scan path: {error}")

    for root, dirs, files in os.walk(project, topdown=True, followlinks=False, onerror=onerror):
        root_path = Path(root)
        result.scanned_dirs += 1

        kept_dirs: list[str] = []
        for name in dirs:
            path = root_path / name
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                except OSError as exc:
                    add_finding(path, "symlink", f"could not resolve symlink safely: {exc}")
                    continue
                if not path_is_same_or_child(target, project):
                    add_finding(path, "symlink", "symlink resolves outside the project")
                    continue
                if contains_sensitive_agent_marker(target) or contains_sensitive_project_marker(project, path):
                    add_finding(path, "symlink", "symlink points at sensitive local state")
                    continue
            if contains_sensitive_project_marker(project, path):
                add_finding(path, "path", project_sensitive_reason(project, path))
                continue
            if name in SECRET_SCAN_SKIP_DIR_NAMES:
                result.skipped_dirs += 1
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in files:
            path = root_path / name
            result.scanned_files += 1
            if result.scanned_files > max_files:
                result.truncated = True
                result.errors.append(f"secret scan exceeded max file limit ({max_files})")
                dirs[:] = []
                break
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                except OSError as exc:
                    add_finding(path, "symlink", f"could not resolve symlink safely: {exc}")
                    continue
                if not path_is_same_or_child(target, project):
                    add_finding(path, "symlink", "symlink resolves outside the project")
                    continue
                if contains_sensitive_agent_marker(target):
                    add_finding(path, "symlink", "symlink points at sensitive local state")
                    continue
            if contains_sensitive_project_marker(project, path):
                add_finding(path, "path", project_sensitive_reason(project, path))
                continue
            try:
                stat_result = path.lstat()
            except OSError as exc:
                add_finding(path, "unreadable", f"could not stat file safely: {exc}")
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                add_finding(path, "special", "non-regular filesystem entry is denied")
                continue
            if stat_result.st_size <= max_content_bytes:
                reason = content_secret_reason(path, max_bytes=max_content_bytes)
                if reason.startswith("could not inspect"):
                    add_finding(path, "unreadable", reason)
                elif reason:
                    add_finding(path, "content", reason)
                result.scanned_content_files += 1

    if result.truncated:
        result.warnings.append("secret scan was truncated before checking every file")
    if result.findings:
        if allow_sensitive_project:
            result.warnings.append("sensitive project override is active; agent-mode may expose files named in findings")
        else:
            result.errors.append("sensitive files or symlinks were found under the project root")
    result.ok = not result.errors
    return result


def sanitized_workspace_slug(project: Path) -> str:
    digest = hashlib.sha256(str(project.resolve()).encode("utf-8", errors="replace")).hexdigest()[:12]
    slug = safety.safe_slug(project.name or "project", default="project", max_length=48)
    return f"{slug}-{digest}"


def is_safe_generated_workspace_path(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return path_is_same_or_child(resolved_path, resolved_root) and resolved_path != resolved_root


def should_skip_sanitized_file(project: Path, path: Path, *, max_content_bytes: int) -> tuple[bool, str]:
    name = path.name.lower()
    if contains_sensitive_project_marker(project, path):
        return True, project_sensitive_reason(project, path)
    if any(name.endswith(suffix) for suffix in SANITIZED_SKIP_FILE_SUFFIXES):
        return True, "archive/database/bulk data files are omitted from sanitized advisor workspace"
    try:
        stat_result = path.lstat()
    except OSError as exc:
        return True, f"could not stat file safely: {exc}"
    if not stat.S_ISREG(stat_result.st_mode):
        return True, "non-regular filesystem entry omitted"
    if stat_result.st_size > max_content_bytes:
        return True, f"file exceeds sanitized content inspection limit ({max_content_bytes} bytes)"
    return False, ""


def sanitized_source_content_limit(*, max_content_bytes: int) -> int:
    source_limit = int(
        os.environ.get(
            SANITIZED_SOURCE_MAX_BYTES_ENV,
            DEFAULT_SANITIZED_SOURCE_MAX_BYTES,
        )
    )
    return max(max_content_bytes, source_limit)


def sanitized_file_content_limit(path: Path, *, max_content_bytes: int) -> int:
    if path.suffix.lower() not in SANITIZED_LARGE_SOURCE_SUFFIXES:
        return max_content_bytes
    return sanitized_source_content_limit(max_content_bytes=max_content_bytes)


def read_regular_file(path: Path, *, max_content_bytes: int) -> tuple[os.stat_result, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise RuntimeError("source entry is not a regular file")
        if stat_result.st_size > max_content_bytes:
            raise RuntimeError(
                f"source file exceeds sanitized content inspection limit ({max_content_bytes} bytes)"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            content = handle.read(max_content_bytes + 1)
        if len(content) > max_content_bytes:
            raise RuntimeError(
                f"source file exceeds sanitized content inspection limit ({max_content_bytes} bytes)"
            )
        return stat_result, content
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_regular_file_beneath(
    root: Path,
    relative: Path,
    *,
    max_content_bytes: int,
) -> tuple[os.stat_result, bytes]:
    """Open a source file without following a swapped parent-directory symlink."""
    if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise RuntimeError("source path is not a safe project-relative file")
    if (
        os.open not in os.supports_dir_fd
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise RuntimeError("secure component-wise source traversal is unavailable on this platform")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    root_descriptor = os.open(root, directory_flags)
    current_descriptor = root_descriptor
    file_descriptor = -1
    try:
        root_stat = os.fstat(root_descriptor)
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=current_descriptor)
            next_stat = os.fstat(next_descriptor)
            if not stat.S_ISDIR(next_stat.st_mode) or next_stat.st_dev != root_stat.st_dev:
                os.close(next_descriptor)
                raise RuntimeError("source path crosses a filesystem boundary")
            if current_descriptor != root_descriptor:
                os.close(current_descriptor)
            current_descriptor = next_descriptor
        file_descriptor = os.open(relative.parts[-1], file_flags, dir_fd=current_descriptor)
        stat_result = os.fstat(file_descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise RuntimeError("source entry is not a regular file")
        if stat_result.st_dev != root_stat.st_dev:
            raise RuntimeError("source file crosses a filesystem boundary")
        if stat_result.st_nlink > 1:
            raise RuntimeError("source file is hardlinked")
        if stat_result.st_size > max_content_bytes:
            raise RuntimeError(
                f"source file exceeds sanitized content inspection limit ({max_content_bytes} bytes)"
            )
        with os.fdopen(file_descriptor, "rb") as handle:
            file_descriptor = -1
            content = handle.read(max_content_bytes + 1)
        if len(content) > max_content_bytes:
            raise RuntimeError(
                f"source file exceeds sanitized content inspection limit ({max_content_bytes} bytes)"
            )
        return stat_result, content
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if current_descriptor != root_descriptor:
            os.close(current_descriptor)
        os.close(root_descriptor)


def sanitized_file_entry(
    project: Path,
    path: Path,
    relative: Path,
    *,
    max_content_bytes: int,
) -> tuple[SanitizedCopyEntry | None, str]:
    skip, reason = should_skip_sanitized_file(
        project,
        path,
        max_content_bytes=max_content_bytes,
    )
    if skip:
        return None, reason
    try:
        stat_result, source_content = read_regular_file_beneath(
            project,
            relative,
            max_content_bytes=max_content_bytes,
        )
    except (OSError, RuntimeError) as exc:
        return None, f"could not inspect file safely: {exc}"
    source_hash = sha256_bytes(source_content)
    redacted_content: bytes | None = None
    redaction_reason = ""
    if file_looks_binary(source_content):
        return None, "binary file omitted because its content cannot be safely redacted"
    text = source_content.decode("utf-8", errors="replace")
    redaction_reason = content_secret_reason_text(text)
    if redaction_reason:
        redacted_text = redact_sanitized_text(text)
        remaining_reason = content_secret_reason_text(redacted_text)
        if remaining_reason:
            return None, f"could not safely redact secret-looking content ({remaining_reason})"
        redacted_content = redacted_text.encode("utf-8")
    target_content = redacted_content if redacted_content is not None else source_content
    return (
        SanitizedCopyEntry(
            source=path,
            relative=relative,
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            mode=stat_result.st_mode,
            source_sha256=source_hash,
            target_sha256=sha256_bytes(target_content),
            redacted_content=redacted_content,
            redaction_reason=redaction_reason,
        ),
        "",
    )


def build_sanitized_copy_plan(project: Path, *, max_content_bytes: int) -> SanitizedCopyPlan:
    plan = SanitizedCopyPlan()
    digest = hashlib.sha256()
    try:
        max_entries = int(
            os.environ.get(
                SANITIZED_MAX_ENTRIES_ENV,
                DEFAULT_SANITIZED_MAX_ENTRIES,
            )
        )
        max_total_bytes = int(
            os.environ.get(
                SANITIZED_MAX_TOTAL_BYTES_ENV,
                DEFAULT_SANITIZED_MAX_TOTAL_BYTES,
            )
        )
    except ValueError:
        plan.errors.append("sanitized workspace limits must be integers")
        return plan
    if max_entries < 1 or max_entries > DEFAULT_SOURCE_TREE_MAX_ENTRIES:
        plan.errors.append(
            f"sanitized workspace entry limit must be between 1 and "
            f"{DEFAULT_SOURCE_TREE_MAX_ENTRIES}"
        )
        return plan
    if max_total_bytes < 1 or max_total_bytes > 16 * 1024 * 1024 * 1024:
        plan.errors.append(
            "sanitized workspace byte limit must be between 1 and 17179869184"
        )
        return plan
    try:
        verify_source_tree_boundary(project)
    except (OSError, RuntimeError) as exc:
        plan.errors.append(f"source boundary validation failed: {exc}")
        return plan

    def record(kind: str, relative: Path, detail: str = "") -> None:
        digest.update(kind.encode("ascii"))
        digest.update(b"\0")
        digest.update(relative.as_posix().encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(detail.encode("utf-8", errors="replace"))
        digest.update(b"\0")

    def remember_skip(path: Path, skip_reason: str) -> None:
        relative = Path(safe_relative_label(project, path))
        record("skip", relative, skip_reason)
        plan.skipped_paths.append(f"{relative.as_posix()}: {skip_reason}")
        if len(plan.skipped_samples) < 12:
            plan.skipped_samples.append(f"{relative.as_posix()}: {skip_reason}")

    for root_dir, dirs, files in os.walk(project, topdown=True, followlinks=False):
        source_root = Path(root_dir)
        try:
            rel_root = source_root.resolve().relative_to(project)
        except ValueError:
            plan.errors.append("sanitized copy encountered a path outside the project")
            break
        dirs.sort()
        files.sort()

        kept_dirs: list[str] = []
        for name in dirs:
            plan.scanned_entries += 1
            if plan.scanned_entries > max_entries:
                plan.errors.append(
                    f"sanitized copy exceeded max entry limit ({max_entries})"
                )
                plan.fingerprint = digest.hexdigest()
                return plan
            source = source_root / name
            if source.is_symlink():
                plan.skipped_symlinks += 1
                remember_skip(source, "symlink omitted")
                continue
            if name in SECRET_SCAN_SKIP_DIR_NAMES:
                plan.skipped_dirs += 1
                remember_skip(source, "generated/dependency directory omitted")
                continue
            if contains_sensitive_project_marker(project, source):
                plan.skipped_dirs += 1
                remember_skip(source, project_sensitive_reason(project, source))
                continue
            kept_dirs.append(name)
            record("dir", rel_root / name)
        dirs[:] = kept_dirs

        for name in files:
            plan.scanned_entries += 1
            if plan.scanned_entries > max_entries:
                plan.errors.append(
                    f"sanitized copy exceeded max entry limit ({max_entries})"
                )
                plan.fingerprint = digest.hexdigest()
                return plan
            source = source_root / name
            if source.is_symlink():
                plan.skipped_symlinks += 1
                remember_skip(source, "symlink omitted")
                continue
            relative = rel_root / name
            if len(relative.parts) == 1 and name in SANITIZED_GENERATED_FILE_NAMES:
                plan.skipped_files += 1
                remember_skip(source, "reserved sanitized-workspace metadata path omitted")
                continue
            file_content_limit = sanitized_file_content_limit(
                source,
                max_content_bytes=max_content_bytes,
            )
            entry, skip_reason = sanitized_file_entry(
                project,
                source,
                relative,
                max_content_bytes=file_content_limit,
            )
            if entry is None:
                if skip_reason.startswith("could not inspect file safely"):
                    plan.errors.append(f"{relative.as_posix()}: {skip_reason}")
                    continue
                plan.skipped_files += 1
                remember_skip(source, skip_reason)
                continue
            target_size = (
                len(entry.redacted_content)
                if entry.redacted_content is not None
                else entry.size
            )
            if plan.copied_bytes + target_size > max_total_bytes:
                plan.errors.append(
                    f"sanitized copy exceeded max aggregate byte limit "
                    f"({max_total_bytes})"
                )
                plan.fingerprint = digest.hexdigest()
                return plan
            plan.copied_bytes += target_size
            plan.entries.append(entry)
            if entry.redacted_content is not None:
                plan.redacted_paths.append(
                    f"{relative.as_posix()}: {entry.redaction_reason}"
                )
            record(
                "file",
                relative,
                f"{entry.target_sha256}:{entry.mode}:"
                f"{'redacted' if entry.redacted_content is not None else 'verbatim'}",
            )
    plan.fingerprint = digest.hexdigest()
    return plan


def populate_sanitized_status_from_manifest(
    status: SanitizedWorkspaceStatus,
    workspace: Path,
    manifest: dict[str, Any],
    *,
    reused: bool,
) -> None:
    status.workspace_dir = str(workspace)
    status.generation_id = str(manifest.get("generation_id") or workspace.name)
    status.source_fingerprint = str(manifest.get("source_fingerprint") or "")
    status.reused = reused
    status.copied_files = int(manifest.get("copied_files") or 0)
    status.copied_dirs = int(manifest.get("copied_dirs") or 0)
    status.redacted_files = int(manifest.get("redacted_files") or 0)
    status.skipped_files = int(manifest.get("skipped_files") or 0)
    status.skipped_dirs = int(manifest.get("skipped_dirs") or 0)
    status.skipped_symlinks = int(manifest.get("skipped_symlinks") or 0)
    if status.skipped_files or status.skipped_dirs or status.skipped_symlinks:
        status.warnings.append("sanitized workspace omits some files from the original checkout")


def reusable_sanitized_generation(
    workspace: Path,
    *,
    fingerprint: str,
    entries: list[SanitizedCopyEntry],
) -> dict[str, Any] | None:
    marker = workspace / "ADVISOR_SANITIZED_WORKSPACE.md"
    manifest_path = workspace / "SANITIZED_WORKSPACE_MANIFEST.json"
    try:
        if (
            not stat.S_ISDIR(workspace.lstat().st_mode)
            or not stat.S_ISREG(marker.lstat().st_mode)
            or not stat.S_ISREG(manifest_path.lstat().st_mode)
        ):
            return None
    except OSError:
        return None
    try:
        _manifest_stat, manifest_bytes = read_regular_file(
            manifest_path,
            max_content_bytes=16 * 1024 * 1024,
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("source_fingerprint") != fingerprint:
        return None
    recorded_manifest_hash = manifest.get("manifest_sha256")
    hash_payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    expected_manifest_hash = sha256_bytes(
        json.dumps(
            hash_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    try:
        marker_hash = sha256_file(marker)
    except (OSError, RuntimeError):
        return None
    if (
        manifest.get("schema_version") != "2.3"
        or manifest.get("fingerprint_scope") != "sanitized-output"
        or not isinstance(recorded_manifest_hash, str)
        or recorded_manifest_hash != expected_manifest_hash
        or manifest.get("marker_sha256") != marker_hash
    ):
        return None
    expected_files = {
        entry.relative.as_posix(): {
            "sha256": entry.target_sha256,
            "mode": 0o500 if entry.mode & 0o111 else 0o400,
        }
        for entry in entries
    }
    try:
        paths = list(workspace.rglob("*"))
        actual_files = {
            path.relative_to(workspace).as_posix()
            for path in paths
            if path.is_file() and not path.is_symlink()
        }
        actual_dirs = {
            path.relative_to(workspace).as_posix()
            for path in paths
            if path.is_dir() and not path.is_symlink()
        }
        expected_dirs: set[str] = set()
        for relative in expected_files:
            parent = Path(relative).parent
            while parent != Path("."):
                expected_dirs.add(parent.as_posix())
                parent = parent.parent
        if any(path.is_symlink() for path in paths):
            return None
        if actual_files != set(expected_files) | SANITIZED_GENERATED_FILE_NAMES:
            return None
        if actual_dirs != expected_dirs:
            return None
        if stat.S_IMODE(workspace.lstat().st_mode) != 0o500:
            return None
        for path in paths:
            relative = path.relative_to(workspace).as_posix()
            actual_mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_dir() and actual_mode != 0o500:
                return None
            if path.is_file():
                expected_mode = (
                    0o400
                    if relative in SANITIZED_GENERATED_FILE_NAMES
                    else int(expected_files[relative]["mode"])
                )
                if actual_mode != expected_mode:
                    return None
        for relative, expected in expected_files.items():
            target = workspace / relative
            if sha256_file(target) != expected["sha256"]:
                return None
    except OSError:
        return None
    clean_scan = scan_project_secrets(workspace)
    if not clean_scan.ok:
        return None
    return manifest


def git_source_provenance(project: Path) -> dict[str, Any]:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return safety.run_hardened_git(
            project,
            list(args),
            text=True,
            timeout=5,
            maximum_output_bytes=4 * 1024 * 1024,
        )

    try:
        head = git("rev-parse", "HEAD")
        tree = git("rev-parse", "HEAD^{tree}")
        status = git(
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return {"available": False}
    if head.returncode != 0 or tree.returncode != 0 or status.returncode != 0:
        return {"available": False}
    return {
        "available": True,
        "head_commit": head.stdout.strip(),
        "head_tree": tree.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
    }


def make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise RuntimeError("sanitized workspace unexpectedly contains a symlink")
        if path.is_dir():
            os.chmod(path, 0o500)
        elif path.is_file():
            source_mode = path.stat().st_mode
            os.chmod(path, 0o500 if source_mode & 0o111 else 0o400)
    os.chmod(root, 0o500)


def make_tree_writable_for_cleanup(root: Path) -> None:
    if root.is_symlink():
        try:
            root.unlink()
        except OSError:
            pass
        return
    if not root.exists():
        return
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            root.unlink(missing_ok=True)
            return
    except OSError:
        return
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in [*files, *dirs]:
            path = Path(current) / name
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    path.unlink(missing_ok=True)
                elif stat.S_ISDIR(mode):
                    os.chmod(path, 0o700, follow_symlinks=False)
                else:
                    os.chmod(path, 0o600, follow_symlinks=False)
            except OSError:
                pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def remove_generated_tree(root: Path) -> None:
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        root.unlink(missing_ok=True)
        return
    make_tree_writable_for_cleanup(root)
    shutil.rmtree(root)


def write_private_sanitization_manifest(
    project_root: Path,
    project: Path,
    plan: SanitizedCopyPlan,
    public_manifest: dict[str, Any],
) -> Path:
    """Persist sensitive omission details outside the MCP-readable generation."""
    generation_id = str(public_manifest.get("generation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{24}", generation_id):
        raise RuntimeError("sanitized generation id is invalid")
    root = project_root / "private-manifests"
    safety.ensure_private_dir(root)
    path = root / f"{generation_id}.json"
    payload = {
        "schema_version": "1.0",
        "source_path_hash": hashlib.sha256(
            str(project).encode("utf-8", errors="replace")
        ).hexdigest(),
        "source_project_name": project.name,
        "source_fingerprint": plan.fingerprint,
        "source_git": public_manifest.get("source_git"),
        "generation_id": generation_id,
        "generated_utc": public_manifest.get("generated_utc"),
        "copied_files": public_manifest.get("copied_files"),
        "copied_dirs": public_manifest.get("copied_dirs"),
        "redacted_files": len(plan.redacted_paths),
        "skipped_files": plan.skipped_files,
        "skipped_dirs": plan.skipped_dirs,
        "skipped_symlinks": plan.skipped_symlinks,
        "skipped_paths": plan.skipped_paths,
        "redacted_paths": plan.redacted_paths,
        "public_manifest_sha256": public_manifest.get("manifest_sha256"),
        "warning": "Private local sanitization audit. Do not expose this file through MCP.",
    }
    safety.atomic_write_json(path, payload)
    os.chmod(path, 0o600)
    return path


def create_sanitized_workspace(
    project_dir: str | Path,
    *,
    mode: str = DEFAULT_SANITIZED_WORKSPACE_MODE,
    workspace_root_path: str | Path | None = None,
    reason: str = "",
    max_content_bytes: int | None = None,
) -> SanitizedWorkspaceStatus:
    project = resolve_path(project_dir)
    mode = (mode or DEFAULT_SANITIZED_WORKSPACE_MODE).strip().lower()
    if mode not in VALID_SANITIZED_WORKSPACE_MODES:
        mode = DEFAULT_SANITIZED_WORKSPACE_MODE
    root = workspace_root(workspace_root_path)
    status = SanitizedWorkspaceStatus(mode, True, str(project), workspace_root=str(root), reason=reason or "sanitized workspace requested")
    max_content_bytes = (
        max_content_bytes
        if max_content_bytes is not None
        else int(os.environ.get(SECRET_SCAN_MAX_BYTES_ENV, DEFAULT_SECRET_SCAN_MAX_BYTES))
    )
    if not project.exists() or not project.is_dir():
        status.errors.append("project directory does not exist or is not a directory")
        return status
    root_result = validate_allowed_root(root)
    if not root.exists():
        try:
            safety.ensure_private_dir(root)
        except OSError as exc:
            status.errors.append(f"could not create sanitized workspace root: {exc}")
            return status
    elif not root_result.ok:
        status.errors.extend(root_result.errors)
        return status
    project_root = root / sanitized_workspace_slug(project)
    generations_root = project_root / "generations"
    status.workspace_allowed_root = str(project_root)
    if not is_safe_generated_workspace_path(generations_root, root):
        status.errors.append("sanitized workspace path is not safely under the workspace root")
        return status
    try:
        safety.ensure_private_dir(generations_root)
    except OSError as exc:
        status.errors.append(f"could not create sanitized workspace generation root: {exc}")
        return status

    lock = concurrency.InterProcessLock(
        project_root / ".workspace.lock",
        timeout=300.0,
        wait_message="Advisor agent-mode is waiting for another session to finish the sanitized workspace snapshot.",
    )
    with lock:
        plan = build_sanitized_copy_plan(project, max_content_bytes=max_content_bytes)
        if plan.errors:
            status.errors.extend(plan.errors)
            return status
        source_git = git_source_provenance(project)
        status.source_fingerprint = plan.fingerprint
        generation_id = plan.fingerprint[:24]
        workspace = generations_root / generation_id
        status.workspace_dir = str(workspace)
        status.generation_id = generation_id

        existing_manifest = reusable_sanitized_generation(
            workspace,
            fingerprint=plan.fingerprint,
            entries=plan.entries,
        )
        if existing_manifest is not None:
            final_plan = build_sanitized_copy_plan(project, max_content_bytes=max_content_bytes)
            final_git = git_source_provenance(project)
            if (
                final_plan.errors
                or final_plan.fingerprint != plan.fingerprint
                or final_git != source_git
                or existing_manifest.get("source_git") != final_git
            ):
                status.errors.append(
                    "source project changed while the sanitized snapshot was being verified; retry"
                )
                return status
            try:
                private_manifest = write_private_sanitization_manifest(
                    project_root,
                    project,
                    final_plan,
                    existing_manifest,
                )
            except (OSError, RuntimeError) as exc:
                status.errors.append(f"could not write private sanitization audit: {exc}")
                return status
            populate_sanitized_status_from_manifest(status, workspace, existing_manifest, reused=True)
            status.private_manifest_path = str(private_manifest)
            return status

        staging = project_root / f".staging-{uuid.uuid4().hex}"
        if not is_safe_generated_workspace_path(staging, root):
            status.errors.append("sanitized staging path is not safely under the workspace root")
            return status
        try:
            safety.ensure_private_dir(staging)
            for entry in plan.entries:
                file_content_limit = sanitized_file_content_limit(
                    project / entry.relative,
                    max_content_bytes=max_content_bytes,
                )
                before, source_content = read_regular_file_beneath(
                    project,
                    entry.relative,
                    max_content_bytes=file_content_limit,
                )
                if (
                    before.st_size != entry.size
                    or before.st_mtime_ns != entry.mtime_ns
                    or before.st_mode != entry.mode
                ):
                    raise RuntimeError("source project changed while the sanitized snapshot was being prepared; retry")
                if sha256_bytes(source_content) != entry.source_sha256:
                    raise RuntimeError("source project content changed while the sanitized snapshot was being prepared; retry")
                target = staging / entry.relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    entry.redacted_content
                    if entry.redacted_content is not None
                    else source_content
                )
                os.chmod(target, entry.mode & 0o777)
                if sha256_file(target) != entry.target_sha256:
                    raise RuntimeError("sanitized workspace content hash verification failed")

            copied_dirs = sum(1 for item in staging.rglob("*") if item.is_dir())
            generated_utc = datetime.now(timezone.utc).isoformat()
            marker_lines = [
                "# Advisor Sanitized Workspace",
                "",
                "This directory is an automatically generated content-hashed, read-only review snapshot for ChatGPT/DevSpace advisor agent-mode.",
                "It omits detected secret-looking material, advisor transcripts, dependency caches, generated build outputs, archives, databases, and symlinks.",
                "Codex must verify final facts against the original checkout before acting on advisor claims.",
                "",
                f"source_project_name: {project.name}",
                f"generation_id: {generation_id}",
                f"generated_utc: {generated_utc}",
                f"copied_files: {len(plan.entries)}",
                f"redacted_files: {len(plan.redacted_paths)}",
                f"skipped_files: {plan.skipped_files}",
                f"skipped_dirs: {plan.skipped_dirs}",
                f"skipped_symlinks: {plan.skipped_symlinks}",
            ]
            marker_text = "\n".join(marker_lines) + "\n"
            manifest = {
                "schema_version": "2.3",
                "scanner": "external-advisor-agent-mode",
                "fingerprint_scope": "sanitized-output",
                "source_path_hash": hashlib.sha256(str(project).encode("utf-8", errors="replace")).hexdigest(),
                "source_project_name": project.name,
                "source_fingerprint": plan.fingerprint,
                "source_git": source_git,
                "generation_id": generation_id,
                "generated_utc": generated_utc,
                "copied_files": len(plan.entries),
                "copied_dirs": copied_dirs,
                "redacted_files": len(plan.redacted_paths),
                "skipped_files": plan.skipped_files,
                "skipped_dirs": plan.skipped_dirs,
                "skipped_symlinks": plan.skipped_symlinks,
                "marker_sha256": sha256_bytes(marker_text.encode("utf-8")),
                "warning": "This is an incomplete content-hashed, read-only sanitized review snapshot. Codex must verify final facts in the original checkout.",
            }
            manifest["manifest_sha256"] = sha256_bytes(
                json.dumps(
                    manifest,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            safety.atomic_write_text(staging / "ADVISOR_SANITIZED_WORKSPACE.md", marker_text)
            safety.atomic_write_json(staging / "SANITIZED_WORKSPACE_MANIFEST.json", manifest)

            clean_scan = scan_project_secrets(
                staging,
                max_content_bytes=sanitized_source_content_limit(
                    max_content_bytes=max_content_bytes,
                ),
            )
            if not clean_scan.ok:
                status.errors.append("sanitized workspace still contains sensitive-looking files")
                for finding in clean_scan.findings[:12]:
                    status.errors.append(f"{finding.path}: {finding.reason}")
                return status
            final_plan = build_sanitized_copy_plan(project, max_content_bytes=max_content_bytes)
            final_git = git_source_provenance(project)
            if (
                final_plan.errors
                or final_plan.fingerprint != plan.fingerprint
                or final_git != source_git
            ):
                raise RuntimeError(
                    "source project changed while the sanitized snapshot was being prepared; retry"
                )
            if workspace.exists() or workspace.is_symlink():
                remove_generated_tree(workspace)
            os.replace(staging, workspace)
            make_tree_read_only(workspace)
            post_publish_plan = build_sanitized_copy_plan(
                project,
                max_content_bytes=max_content_bytes,
            )
            if (
                post_publish_plan.errors
                or post_publish_plan.fingerprint != plan.fingerprint
                or git_source_provenance(project) != source_git
            ):
                raise RuntimeError(
                    "source project changed before the sanitized snapshot could be returned; retry"
                )
            private_manifest = write_private_sanitization_manifest(
                project_root,
                project,
                post_publish_plan,
                manifest,
            )
            populate_sanitized_status_from_manifest(status, workspace, manifest, reused=False)
            status.private_manifest_path = str(private_manifest)
        except (OSError, RuntimeError) as exc:
            status.errors.append(f"could not create sanitized workspace generation: {exc}")
        finally:
            if staging.exists() or staging.is_symlink():
                try:
                    remove_generated_tree(staging)
                except OSError:
                    pass
    return status


def validate_allowed_root(raw_root: str | Path, *, case_insensitive: bool = False) -> ValidationResult:
    raw_text = str(raw_root)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        root = resolve_path(raw_root)
    except OSError as exc:
        return ValidationResult(False, raw_text, raw_text, [f"could not resolve root: {exc}"], [])
    if not root.exists():
        errors.append("allowed root does not exist")
    elif not root.is_dir():
        errors.append("allowed root is not a directory")
    if is_drive_or_filesystem_root(root):
        errors.append("allowed root is too broad")
    home = Path.home().resolve()
    if path_key(root, case_insensitive=case_insensitive) == path_key(home, case_insensitive=case_insensitive):
        errors.append("home directory is too broad")
    if contains_sensitive_agent_marker(root):
        errors.append(sensitive_reason(root))
    if root.name.lower() in {"documents", "downloads", "desktop"} and root.parent == home:
        warnings.append("home-level document folders are broad; prefer one project or one narrow workspace root")
    return ValidationResult(not errors, raw_text, str(root), errors, warnings)


def validate_project_under_allowed_root(
    project_dir: str | Path,
    allowed_root: str | Path,
    *,
    case_insensitive: bool = False,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        project = resolve_path(project_dir)
        root = resolve_path(allowed_root)
    except OSError as exc:
        return ValidationResult(False, str(project_dir), str(project_dir), [f"could not resolve project/root: {exc}"], [])
    root_result = validate_allowed_root(root, case_insensitive=case_insensitive)
    errors.extend(root_result.errors)
    warnings.extend(root_result.warnings)
    if not project.exists():
        errors.append("project directory does not exist")
    elif not project.is_dir():
        errors.append("project path is not a directory")
    if contains_sensitive_agent_marker(project):
        errors.append(sensitive_reason(project))
    if not path_is_same_or_child(project, root, case_insensitive=case_insensitive):
        errors.append("project directory is not inside the allowed root after resolving symlinks")
    if path_key(project, case_insensitive=case_insensitive) == path_key(root, case_insensitive=case_insensitive):
        warnings.append("allowed root is exactly the project directory; this is narrow and preferred")
    return ValidationResult(not errors, str(project_dir), str(project), errors, warnings)


def select_allowed_root(
    project_dir: Path,
    allowed_roots: list[str],
    *,
    case_insensitive: bool = False,
) -> tuple[str, list[ValidationResult]]:
    results: list[ValidationResult] = []
    for root in allowed_roots:
        result = validate_project_under_allowed_root(project_dir, root, case_insensitive=case_insensitive)
        results.append(result)
        if result.ok:
            return str(resolve_path(root)), results
    return "", results


def check_bridge_executable(
    executable: str,
    *,
    project_dir: Path,
    allow_project_bridge: bool = False,
    case_insensitive: bool = False,
) -> BridgeStatus:
    resolved = shutil.which(executable)
    if not resolved:
        candidate = Path(executable).expanduser()
        if candidate.exists():
            resolved = str(candidate.resolve())
    if not resolved:
        return BridgeStatus(False, executable, "", [f"MCP bridge executable not found: {executable}"], [])
    path = Path(resolved).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not path.exists() or not path.is_file():
        errors.append("bridge executable path does not resolve to a file")
    if not os.access(path, os.X_OK):
        errors.append("bridge executable is not executable")
    if not allow_project_bridge and path_is_same_or_child(path, project_dir.resolve(), case_insensitive=case_insensitive):
        errors.append("bridge executable resolves inside the project; refusing possible project-local shim")
    parts = relative_parts(path)
    if not allow_project_bridge and "node_modules" in parts and ".bin" in parts:
        errors.append("bridge executable resolves through node_modules/.bin; use a trusted global/local path or explicit override")
    if contains_sensitive_agent_marker(path):
        errors.append(sensitive_reason(path))
    if executable == DEFAULT_BRIDGE_EXECUTABLE and path.name != DEFAULT_BRIDGE_EXECUTABLE:
        warnings.append("bridge executable was resolved through PATH")
    return BridgeStatus(not errors, executable, str(path), errors, warnings)


def run_small_command(project_dir: Path, command: list[str], timeout: int = 5) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=project_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return completed.returncode == 0, safety.truncate(safety.redact_sensitive_text(output), 400)


def check_worktree(project_dir: Path) -> WorktreeStatus:
    def git_check(arguments: list[str]) -> tuple[bool, str]:
        try:
            completed = safety.run_hardened_git(
                project_dir,
                arguments,
                text=True,
                timeout=5,
                maximum_output_bytes=1024 * 1024,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        output = (completed.stdout + "\n" + completed.stderr).strip()
        return (
            completed.returncode == 0,
            safety.truncate(safety.redact_sensitive_text(output), 400),
        )

    inside, inside_output = git_check(["rev-parse", "--is-inside-work-tree"])
    has_head, head_output = git_check(["rev-parse", "--verify", "HEAD"])
    errors: list[str] = []
    if not inside:
        errors.append(inside_output or "not inside a Git work tree")
    if inside and not has_head:
        errors.append(head_output or "Git repository has no HEAD commit")
    return WorktreeStatus(inside and has_head, inside, has_head, errors)


def node_status() -> dict[str, Any]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    npx = shutil.which("npx")
    node_version = ""
    if node:
        ok, output = run_small_command(Path.cwd(), [node, "--version"])
        node_version = output.splitlines()[0] if ok and output else ""
    return {
        "node": node or "",
        "node_version": node_version,
        "npm": npm or "",
        "npx": npx or "",
    }


def evaluate_agent_mode(
    project_dir: Path,
    *,
    mode: str = DEFAULT_AGENT_MODE,
    allowed_roots: list[str] | None = None,
    bridge_executable: str = DEFAULT_BRIDGE_EXECUTABLE,
    require_bridge: bool = True,
    allow_project_bridge: bool = False,
    allow_sensitive_project: bool = False,
    secret_scan: bool = True,
    sanitized_workspace_mode: str = DEFAULT_SANITIZED_WORKSPACE_MODE,
    workspace_root_path: str | Path | None = None,
    config: str | Path | None = None,
    case_insensitive: bool | None = None,
) -> AgentModeStatus:
    case_insensitive = default_case_insensitive() if case_insensitive is None else case_insensitive
    mode = (mode or DEFAULT_AGENT_MODE).strip().lower()
    if mode not in VALID_AGENT_MODES:
        mode = DEFAULT_AGENT_MODE
    project = project_dir.resolve()
    cfg_path = config_path(config)
    if path_is_same_or_child(cfg_path, project, case_insensitive=case_insensitive):
        status = AgentModeStatus(mode, False, str(project), [])
        status.errors.append("agent config path must live outside the project being exposed")
        return status
    roots = allowed_roots or configured_allowed_roots(cfg_path)
    status = AgentModeStatus(mode, False, str(project), roots)
    if mode == "off":
        status.notes.append("agent-mode disabled by configuration")
        return status
    if not roots:
        status.errors.append(f"no allowed roots configured; set {ALLOWED_ROOTS_ENV} or pass --allowed-root")
        return status
    selected, root_results = select_allowed_root(project, roots, case_insensitive=case_insensitive)
    for result in root_results:
        status.warnings.extend(result.warnings)
    if not selected:
        for result in root_results:
            if result.errors:
                status.errors.append(f"{result.path}: " + "; ".join(result.errors))
        if not status.errors:
            status.errors.append("project is not under any configured allowed root")
        return status
    status.selected_root = selected
    agent_project = project
    sanitized_workspace_mode = (sanitized_workspace_mode or DEFAULT_SANITIZED_WORKSPACE_MODE).strip().lower()
    if sanitized_workspace_mode not in VALID_SANITIZED_WORKSPACE_MODES:
        sanitized_workspace_mode = DEFAULT_SANITIZED_WORKSPACE_MODE
    if secret_scan:
        scan = scan_project_secrets(project, allow_sensitive_project=allow_sensitive_project)
        status.secret_scan = scan
        status.warnings.extend(scan.warnings)
        needs_sanitized = (not scan.ok or sanitized_workspace_mode == "always") and not allow_sensitive_project
        if needs_sanitized and sanitized_workspace_mode != "off":
            reason = "source project has sensitive preflight findings" if not scan.ok else "sanitized workspace mode is always"
            sanitized = create_sanitized_workspace(
                project,
                mode=sanitized_workspace_mode,
                workspace_root_path=workspace_root_path,
                reason=reason,
            )
            status.sanitized_workspace = sanitized
            status.warnings.extend(sanitized.warnings)
            if sanitized.errors or not sanitized.workspace_dir:
                status.errors.extend(scan.errors)
                status.errors.extend(sanitized.errors)
                for finding in scan.findings[:12]:
                    status.errors.append(f"{finding.path}: {finding.reason}")
                if len(scan.findings) > 12:
                    status.errors.append(f"... {len(scan.findings) - 12} more sensitive findings omitted")
                return status
            agent_project = Path(sanitized.workspace_dir).resolve()
            status.notes.append("using sanitized review workspace instead of the original checkout")
            status.selected_root = sanitized.workspace_dir
        elif not scan.ok:
            status.errors.extend(scan.errors)
            for finding in scan.findings[:12]:
                status.errors.append(f"{finding.path}: {finding.reason}")
            if len(scan.findings) > 12:
                status.errors.append(f"... {len(scan.findings) - 12} more sensitive findings omitted")
            return status
        if allow_sensitive_project and scan.findings:
            status.warnings.append("agent-mode sensitive-project override is active for this run")
    else:
        status.warnings.append("secret preflight scan disabled by explicit configuration")
        if sanitized_workspace_mode == "always":
            sanitized = create_sanitized_workspace(
                project,
                mode=sanitized_workspace_mode,
                workspace_root_path=workspace_root_path,
                reason="sanitized workspace mode is always",
            )
            status.sanitized_workspace = sanitized
            status.warnings.extend(sanitized.warnings)
            if sanitized.errors or not sanitized.workspace_dir:
                status.errors.extend(sanitized.errors)
                return status
            agent_project = Path(sanitized.workspace_dir).resolve()
            status.selected_root = sanitized.workspace_dir
    bridge = check_bridge_executable(
        bridge_executable,
        project_dir=agent_project,
        allow_project_bridge=allow_project_bridge,
        case_insensitive=case_insensitive,
    )
    status.bridge = bridge
    status.worktree = check_worktree(agent_project)
    if bridge.warnings:
        status.warnings.extend(bridge.warnings)
    if require_bridge and not bridge.ok:
        status.errors.extend(bridge.errors)
        return status
    if not require_bridge and not bridge.ok:
        status.warnings.extend(bridge.errors)
    status.notes.append(
        "connector startup requires the patched DevSpace read-only tool mode before exposing this workspace"
    )
    status.available = True
    if agent_project != project:
        status.project_dir = str(agent_project)
    status.notes.append("agent-mode handoff is available for this project")
    return status


def configured_allowed_roots(config: str | Path | None = None) -> list[str]:
    return merge_roots(
        [],
        [
            *split_roots(os.environ.get(ALLOWED_ROOTS_ENV), os.environ.get("DEVSPACE_ALLOWED_ROOTS")),
            *config_allowed_roots(config),
        ],
        case_insensitive=default_case_insensitive(),
    )


def render_status_text(status: AgentModeStatus, *, include_node: bool = True) -> str:
    lines = ["Advisor Agent Mode Doctor"]
    lines.append(f"available: {'yes' if status.available else 'no'}")
    lines.append(f"requested_mode: {status.requested_mode}")
    lines.append(f"project_dir: {status.project_dir}")
    if status.selected_root:
        lines.append(f"selected_allowed_root: {status.selected_root}")
    if status.allowed_roots:
        lines.append("allowed_roots:")
        for root in status.allowed_roots:
            lines.append(f"- {root}")
    else:
        lines.append("allowed_roots: none")
    if status.bridge:
        lines.append(f"bridge_executable: {status.bridge.executable}")
        lines.append(f"bridge_path: {status.bridge.resolved_path or 'not found'}")
        lines.append(f"bridge_ok: {'yes' if status.bridge.ok else 'no'}")
    if status.secret_scan:
        scan = status.secret_scan
        lines.append(f"secret_scan_ok: {'yes' if scan.ok else 'no'}")
        lines.append(f"secret_scan_files: {scan.scanned_files}")
        lines.append(f"secret_scan_dirs: {scan.scanned_dirs}")
        if scan.findings:
            lines.append("secret_findings:")
            for finding in scan.findings[:12]:
                lines.append(f"- {finding.path}: {finding.reason}")
            if len(scan.findings) > 12:
                lines.append(f"- ... {len(scan.findings) - 12} more omitted")
    if status.sanitized_workspace:
        sanitized = status.sanitized_workspace
        lines.append(f"sanitized_workspace_used: {'yes' if sanitized.used else 'no'}")
        lines.append(f"sanitized_workspace_dir: {sanitized.workspace_dir or 'none'}")
        lines.append(f"sanitized_workspace_allowed_root: {sanitized.workspace_allowed_root or 'none'}")
        lines.append(f"sanitized_workspace_generation: {sanitized.generation_id or 'none'}")
        lines.append(f"sanitized_workspace_reused: {'yes' if sanitized.reused else 'no'}")
        lines.append(f"sanitized_workspace_reason: {sanitized.reason or 'none'}")
        lines.append(f"sanitized_copied_files: {sanitized.copied_files}")
        lines.append(f"sanitized_skipped_files: {sanitized.skipped_files}")
        lines.append(f"sanitized_skipped_dirs: {sanitized.skipped_dirs}")
        lines.append(f"sanitized_skipped_symlinks: {sanitized.skipped_symlinks}")
    if include_node:
        nodes = node_status()
        lines.append(f"node: {nodes['node'] or 'not found'}")
        lines.append(f"node_version: {nodes['node_version'] or 'unknown'}")
        lines.append(f"npm: {nodes['npm'] or 'not found'}")
        lines.append(f"npx: {nodes['npx'] or 'not found'}")
    if status.worktree:
        lines.append(f"worktree_available: {'yes' if status.worktree.available else 'no'}")
    if status.errors:
        lines.append("errors:")
        for item in status.errors:
            lines.append(f"- {item}")
    if status.warnings:
        lines.append("warnings:")
        for item in status.warnings:
            lines.append(f"- {item}")
    if status.notes:
        lines.append("notes:")
        for item in status.notes:
            lines.append(f"- {item}")
    lines.append("dry_run: no tunnel opened, no DevSpace server launched, no npx package invoked, no ChatGPT request made")
    return "\n".join(lines)


def handoff_prompt(status: AgentModeStatus, *, task: str = "", worktree: bool = True) -> str:
    if not status.available or not status.selected_root:
        raise RuntimeError("Agent mode is not available for this project.")
    # The read-only DevSpace surface accepts checkout mode only. A sanitized
    # snapshot describes what is mounted, not an open_workspace mode.
    del worktree
    task = safety.truncate(safety.redact_sensitive_text(task.strip()), 3000)
    workspace = {"path": status.project_dir, "mode": "checkout"}
    sanitized_used = bool(status.sanitized_workspace and status.sanitized_workspace.used)
    workspace_json = json.dumps(workspace, indent=2)
    lines = [
        "# Advisor Agent-Mode Handoff",
        "",
        "You are connected through a DevSpace-compatible MCP bridge as a repo-aware advisor.",
        "Your job is to inspect, plan, and review. Codex remains the implementer by default.",
        "",
        "Open exactly one workspace with this MCP call:",
        "",
        "```json",
        workspace_json,
        "```",
        "",
        "Rules:",
        "- Review-only default: do not write, edit, apply patches, run shell commands, start servers, install packages, or change git state unless the user explicitly grants that authority in this chat.",
        "- Inspect first. Read `AGENTS.md`, README, relevant tests, manifests, and nearby implementation before giving advice.",
        "- Do not read, print, summarize, or request secrets: `.env*`, HAR files, cookies, tokens, private keys, wallet files, browser profiles, `.codex-advisor`, or unrelated private files.",
        "- Use the narrow opened workspace only. Do not ask for `~`, `/`, drive roots, browser profiles, or secret stores.",
        (
            "- This checkout-mode path is a generated sanitized snapshot. It intentionally omits detected secrets, advisor transcripts, dependency caches, archives, databases, and symlinks. Do not ask for the original checkout; ask Codex to verify missing facts locally instead."
            if sanitized_used
            else "- This connector is checkout-only. Do not request a worktree or baseRef."
        ),
        "- Treat shell tools as local-machine access. If shell is needed, ask first and explain the exact command.",
        "- Return evidence-backed critique: what you inspected, risks, concrete recommendations, and which claims still need Codex verification.",
        "",
        "Codex task for critique:",
        "",
        task or "(No task text supplied. Ask Codex/user for the specific question before inspecting deeply.)",
    ]
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--doctor", action="store_true", help="Validate local prerequisites without launching networked services.")
    action.add_argument("--print-handoff", action="store_true", help="Print a review-first ChatGPT/DevSpace handoff prompt.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project directory to expose through agent-mode.")
    parser.add_argument("--allowed-root", action="append", default=[], help="Allowed local root. Can be passed multiple times.")
    parser.add_argument("--agent-mode", choices=sorted(VALID_AGENT_MODES), default=os.environ.get(AGENT_MODE_ENV, DEFAULT_AGENT_MODE))
    parser.add_argument("--bridge-executable", default=os.environ.get(BRIDGE_EXECUTABLE_ENV, DEFAULT_BRIDGE_EXECUTABLE))
    parser.add_argument("--no-require-bridge", action="store_true", help="Warn instead of failing when the bridge executable is missing.")
    parser.add_argument("--allow-project-bridge", action="store_true", help="Allow bridge executable paths inside the project.")
    parser.add_argument("--allow-sensitive-project", action="store_true", help="Allow agent-mode even when the project secret preflight finds sensitive paths. Diagnostic only.")
    parser.add_argument("--no-secret-scan", action="store_true", help="Disable the project secret preflight scan. Diagnostic only.")
    parser.add_argument(
        "--sanitized-workspace",
        choices=sorted(VALID_SANITIZED_WORKSPACE_MODES),
        default=os.environ.get(SANITIZED_WORKSPACE_ENV, DEFAULT_SANITIZED_WORKSPACE_MODE),
        help="Create a sanitized review copy automatically, always, or never.",
    )
    parser.add_argument("--workspace-root", help="Root for generated sanitized workspaces. Defaults to ~/.codex/advisor-agent/workspaces.")
    parser.add_argument("--config-path", help="User-level advisor agent config path. Defaults to ~/.codex/advisor-agent/config.json.")
    parser.add_argument("--case-insensitive-paths", action="store_true", help="Use case-insensitive path containment checks.")
    parser.add_argument("--task", help="Task text to include in the generated handoff.")
    parser.add_argument("--task-stdin", action="store_true", help="Read handoff task text from stdin.")
    parser.add_argument("--checkout", action="store_true", help="Do not prefer worktree mode in the handoff.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    roots = args.allowed_root or configured_allowed_roots(args.config_path)
    require_bridge = not args.no_require_bridge and not falsey(os.environ.get(REQUIRE_BRIDGE_ENV))
    allow_project_bridge = args.allow_project_bridge or truthy(os.environ.get(ALLOW_PROJECT_BRIDGE_ENV))
    allow_sensitive_project = args.allow_sensitive_project or truthy(os.environ.get(ALLOW_SENSITIVE_PROJECT_ENV))
    scan_enabled = not args.no_secret_scan and not falsey(os.environ.get(SECRET_SCAN_ENV))
    case_insensitive = args.case_insensitive_paths or default_case_insensitive()
    status = evaluate_agent_mode(
        args.project_dir,
        mode=args.agent_mode,
        allowed_roots=roots,
        bridge_executable=args.bridge_executable,
        require_bridge=require_bridge,
        allow_project_bridge=allow_project_bridge,
        allow_sensitive_project=allow_sensitive_project,
        secret_scan=scan_enabled,
        sanitized_workspace_mode=args.sanitized_workspace,
        workspace_root_path=args.workspace_root,
        config=args.config_path,
        case_insensitive=case_insensitive,
    )
    if args.doctor:
        payload = {"agent_mode": status.to_dict(), "local_tools": node_status()}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(render_status_text(status))
        return 0 if status.available else 2
    task = args.task or ""
    if args.task_stdin:
        task = sys.stdin.read()
    if args.json:
        payload = status.to_dict()
        payload["handoff"] = handoff_prompt(status, task=task, worktree=not args.checkout) if status.available else ""
        print(json.dumps(payload, indent=2))
        return 0 if status.available else 2
    if not status.available:
        print(render_status_text(status, include_node=False), file=sys.stderr)
        return 2
    print(handoff_prompt(status, task=task, worktree=not args.checkout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
