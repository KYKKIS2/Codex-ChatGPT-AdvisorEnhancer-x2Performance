#!/usr/bin/env python3
"""Manage a permanent Cloudflare Access protected, sandboxed DevSpace MCP origin."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_HOSTNAME = ""
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "advisor-domain-mcp"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "advisor-domain-mcp"
# The system cloudflared unit keeps ProtectHome=true, which hides /run/user.
# Use a root-provisioned, user-owned runtime that remains visible in that mount
# namespace without weakening cloudflared's service sandbox.
DEFAULT_RUNTIME_DIR = Path(f"/run/advisor-domain-mcp-{os.getuid()}")
ORIGIN_RUNTIME_NAME = "origin"
GATEWAY_RUNTIME_NAME = "gateway"
ORIGIN_UNIT = "advisor-domain-mcp-origin.service"
GATEWAY_UNIT = "advisor-domain-mcp-gateway.service"
EXPIRY_SERVICE_UNIT = "advisor-domain-mcp-expiry.service"
EXPIRY_TIMER_UNIT = "advisor-domain-mcp-expiry.timer"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
CONFIG_SCHEMA = "2.0"
RUNTIME_SECRET_NAME = "origin-secret"
PUBLIC_GATEWAY_SOCKET_NAME = "cloudflare.sock"
DEFAULT_SESSION_MINUTES = 60
MIN_SESSION_MINUTES = 5
MAX_SESSION_MINUTES = 8 * 60
DEFAULT_ORIGIN_MEMORY_MB = 2048
DEFAULT_GATEWAY_MEMORY_MB = 256
DEFAULT_ORIGIN_CPU_PERCENT = 200
DEFAULT_GATEWAY_CPU_PERCENT = 50
DEFAULT_MAX_FILE_MB = 512
DEFAULT_MIN_FREE_SPACE_MB = 5 * 1024
DEFAULT_MIN_FREE_INODES = 10_000
DEFAULT_MAX_CONCURRENT = 8
MAX_CONCURRENT_LIMIT = 64
DISK_GUARD_INTERVAL_SECONDS = 0.25
STARTUP_READY_TIMEOUT_SECONDS = 5 * 60
REMOTE_HARDENING_MAX_AGE_HOURS = 24
CLOUDFLARE_HARDENING_PROFILE = 6
CLOUDFLARE_API_ROOT = "https://api.cloudflare.com/client/v4"
MAX_CLOUDFLARE_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CLOUDFLARE_PAGES = 100
MAX_CLOUDFLARE_LIST_ITEMS = 100_000
MAX_EXPOSED_TREE_ENTRIES = 500_000
MAX_GIT_PATH_BYTES = 64 * 1024 * 1024
NVIDIA_DEVICE_PATTERN = re.compile(
    r"/dev/nvidia(?:ctl|[0-9]+|-(?:uvm|uvm-tools|modeset))"
)
MAX_CLOUDFLARED_DIAGNOSTIC_BYTES = 256 * 1024
LOCAL_TUNNEL_ID_PATH = Path("/etc/cloudflared/advisor-tunnel-id")
SAFE_ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}
FULL_ACCESS_BULK_PATH_SCAN_ROOTS = (
    ".git",
    ".codex-advisor",
    ".venv",
    "venv",
    "node_modules",
    "artifacts",
    "data",
    "datasets",
    "checkpoints",
    "models",
    "outputs",
    "runs",
)
_AGENT_MODE_MODULE: Any | None = None


class DomainMcpError(RuntimeError):
    pass


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def require_system_runtime_base(path: Path = DEFAULT_RUNTIME_DIR) -> Path:
    if path != DEFAULT_RUNTIME_DIR:
        raise DomainMcpError("Domain MCP runtime base is not the pinned system path.")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DomainMcpError(
            f"System runtime base is missing: {path}. Install its systemd-tmpfiles "
            "entry with the documented one-time sudo command, then rerun prepare."
        ) from exc
    except OSError as exc:
        raise DomainMcpError(f"Could not inspect system runtime base: {path}") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DomainMcpError(
            "System runtime base must be a non-symlink directory owned by the "
            "current uid/gid with mode 0700."
        )
    return path


def cloudflared_socket_namespace_compatible(config: dict[str, Any]) -> bool:
    runtime = Path(str(config.get("runtimeDir") or ""))
    gateway_runtime = Path(str(config.get("gatewayRuntimeDir") or ""))
    gateway_socket = Path(str(config.get("gatewaySocket") or ""))
    return (
        runtime == DEFAULT_RUNTIME_DIR
        and gateway_runtime == DEFAULT_RUNTIME_DIR / GATEWAY_RUNTIME_NAME
        and gateway_socket == gateway_runtime / PUBLIC_GATEWAY_SOCKET_NAME
    )


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


@contextmanager
def lifecycle_lock(config_path: Path) -> Any:
    lock_path = config_path.parent / "lifecycle.lock"
    private_dir(lock_path.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise DomainMcpError("Domain MCP lifecycle lock is not an owner-only regular file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def read_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DomainMcpError(f"Domain MCP config is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise DomainMcpError(f"Domain MCP config is not a regular file: {path}")
    if metadata.st_mode & 0o077:
        raise DomainMcpError(f"Domain MCP config is not private (expected mode 600): {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise DomainMcpError(f"Domain MCP config is not owned by the current user: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainMcpError(f"Could not read domain MCP config: {exc}") from exc
    if not isinstance(value, dict):
        raise DomainMcpError("Domain MCP config must contain a JSON object.")
    return value


def require_executable(name: str) -> Path:
    resolved = shutil.which(name)
    if not resolved:
        raise DomainMcpError(f"Required executable not found: {name}")
    return Path(resolved).resolve()


def validate_runtime_executable(path: Path, project: Path, name: str) -> None:
    try:
        path.relative_to(project)
    except ValueError:
        pass
    else:
        raise DomainMcpError(f"Refusing to use {name} from inside the exposed checkout.")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or metadata.st_uid not in {0, os.getuid()}
    ):
        raise DomainMcpError(
            f"{name} must resolve to a root- or user-owned executable that is not group/world writable."
        )


def harden_user_owned_runtime_file(path: Path) -> None:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        return
    hardened_mode = stat.S_IMODE(metadata.st_mode) & ~0o022
    if hardened_mode != stat.S_IMODE(metadata.st_mode):
        os.chmod(path, hardened_mode)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_regular_file_record(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise DomainMcpError("Checkout fingerprint target is not a regular file.")
        if before.st_nlink > 1:
            raise DomainMcpError("The exposed checkout contains a hardlinked regular file.")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise DomainMcpError("A checkout file changed while it was fingerprinted.") from exc
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(
        getattr(before, field) != getattr(after, field)
        or getattr(after, field) != getattr(current, field)
        for field in stable_fields
    ):
        raise DomainMcpError("A checkout file changed while it was fingerprinted.")
    return {
        "type": "file",
        "mode": stat.S_IMODE(after.st_mode),
        "size": after.st_size,
        "sha256": digest.hexdigest(),
    }


def runtime_integrity_manifest(config: dict[str, Any]) -> dict[str, str]:
    node = Path(config["nodeRoot"]) / "bin" / "node"
    devspace = Path(config["devspaceDist"])
    paths = {
        "bwrap": Path(config["bwrapPath"]),
        "python": Path(config["pythonExecutable"]),
        "git": Path(config["gitExecutable"]),
        "node": node,
        "devspaceCli": devspace / "cli.js",
        "devspaceConfig": devspace / "config.js",
        "devspacePiTools": devspace / "pi-tools.js",
        "devspaceRoots": devspace / "roots.js",
        "devspaceServer": devspace / "server.js",
        "manager": Path(config["managerScript"]),
        "agentMode": Path(config["agentModeScript"]),
        "advisorConcurrency": Path(config["advisorConcurrencyScript"]),
        "advisorSafety": Path(config["advisorSafetyScript"]),
        "readonlyPatch": Path(config["readonlyPatchScript"]),
        "secureOriginPatch": Path(config["secureOriginPatchScript"]),
        "gateway": Path(config["gatewayScript"]),
        "secureServer": Path(config["secureServerScript"]),
        "shellSandbox": Path(config["shellSandboxScript"]),
    }
    manifest = {
        name: file_sha256(path.resolve(strict=True))
        for name, path in paths.items()
    }
    entries = 0
    total_bytes = 0
    for path in sorted(devspace.rglob("*"), key=lambda value: value.as_posix()):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        entries += 1
        total_bytes += metadata.st_size
        if entries > 4096 or total_bytes > 256 * 1024 * 1024:
            raise DomainMcpError("DevSpace runtime integrity scope exceeded its safety limit.")
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink > 1
            or metadata.st_mode & 0o022
            or metadata.st_uid not in {0, os.getuid()}
        ):
            raise DomainMcpError("DevSpace runtime contains an unsafe file.")
        relative = path.relative_to(devspace).as_posix()
        manifest[f"devspaceDist/{relative}"] = file_sha256(path)
    package_json = devspace.parent / "package.json"
    if package_json.is_file() and not package_json.is_symlink():
        package_metadata = package_json.lstat()
        if (
            package_metadata.st_nlink > 1
            or package_metadata.st_mode & 0o022
            or package_metadata.st_uid not in {0, os.getuid()}
        ):
            raise DomainMcpError("DevSpace package metadata is unsafe.")
        manifest["devspacePackage"] = file_sha256(package_json.resolve(strict=True))
    return manifest


def filesystem_capacity_healthy(
    paths: list[Path],
    *,
    minimum_free_bytes: int,
    minimum_free_inodes: int,
) -> bool:
    checked_devices: set[int] = set()
    for path in paths:
        metadata = path.stat()
        if metadata.st_dev in checked_devices:
            continue
        checked_devices.add(metadata.st_dev)
        capacity = os.statvfs(path)
        if capacity.f_bavail * capacity.f_frsize < minimum_free_bytes:
            return False
        if capacity.f_favail > 0 and capacity.f_favail < minimum_free_inodes:
            return False
    return True


def resolve_project(value: str) -> tuple[Path, os.stat_result]:
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise DomainMcpError("The project root itself must not be a symlink.")
    project = supplied.resolve(strict=True)
    metadata = project.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise DomainMcpError("The project root must be a directory.")
    if project in {Path("/"), Path.home().resolve()}:
        raise DomainMcpError("Refusing to expose the filesystem root or the whole home directory.")
    git_dir = project / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise DomainMcpError(
            "Direct full-access mode requires the main Git checkout with a real .git directory."
        )
    return project, metadata


def decode_mountinfo_path(value: str) -> str:
    try:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )
    except ValueError as exc:
        raise DomainMcpError("The kernel mount table contained an invalid path escape.") from exc


def descendant_mount_points(
    project: Path,
    *,
    mountinfo_text: str | None = None,
) -> list[Path]:
    project = project.resolve(strict=True)
    if mountinfo_text is None:
        try:
            raw = Path("/proc/self/mountinfo").read_bytes()
        except OSError as exc:
            raise DomainMcpError("Could not inspect the process mount table.") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise DomainMcpError("The process mount table exceeded its safety limit.")
        mountinfo_text = raw.decode("utf-8", errors="surrogateescape")

    descendants: set[Path] = set()
    for line in mountinfo_text.splitlines():
        fields = line.split(" ")
        if len(fields) < 6 or " - " not in line:
            raise DomainMcpError("The process mount table was malformed.")
        decoded = decode_mountinfo_path(fields[4])
        mount_point = Path(os.path.normpath(decoded))
        if not mount_point.is_absolute():
            raise DomainMcpError("The process mount table contained a relative mount point.")
        try:
            relative = mount_point.relative_to(project)
        except ValueError:
            continue
        if relative != Path("."):
            descendants.add(mount_point)
    return sorted(descendants, key=str)


def run_find_bytes(
    project: Path,
    expression: list[str],
    *,
    maximum_bytes: int,
) -> bytes:
    find = require_executable("find")
    validate_runtime_executable(find, project, "Find")
    try:
        completed = subprocess.run(
            [str(find), "-P", str(project), "-xdev", *expression],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainMcpError("The exposed checkout metadata scan did not complete.") from exc
    if completed.returncode != 0:
        raise DomainMcpError("The exposed checkout changed during its metadata scan.")
    if len(completed.stdout) > maximum_bytes or len(completed.stderr) > 64 * 1024:
        raise DomainMcpError("The exposed checkout metadata scan exceeded its safety limit.")
    return completed.stdout


def verify_exposed_tree_boundary(
    project: Path,
    *,
    mountinfo_text: str | None = None,
) -> None:
    mounts = descendant_mount_points(project, mountinfo_text=mountinfo_text)
    if mounts:
        relative = mounts[0].relative_to(project)
        raise DomainMcpError(
            f"The exposed checkout contains a descendant mount point at {relative}; "
            "unmount it before enabling full access."
        )

    entries = run_find_bytes(
        project,
        ["-mindepth", "1", "-printf", "."],
        maximum_bytes=MAX_EXPOSED_TREE_ENTRIES + 1,
    )
    if len(entries) > MAX_EXPOSED_TREE_ENTRIES:
        raise DomainMcpError("The exposed checkout exceeded the boundary-scan entry limit.")

    violation = run_find_bytes(
        project,
        [
            "-mindepth",
            "1",
            "(",
            "(",
            "-type",
            "f",
            "-links",
            "+1",
            ")",
            "-o",
            "(",
            "!",
            "-type",
            "f",
            "!",
            "-type",
            "d",
            "!",
            "-type",
            "l",
            ")",
            ")",
            "-printf",
            "%P\\0",
            "-quit",
        ],
        maximum_bytes=MAX_GIT_PATH_BYTES,
    )
    if not violation:
        return
    encoded_relative = violation.split(b"\0", 1)[0]
    relative = Path(os.fsdecode(encoded_relative))
    if relative.is_absolute() or ".." in relative.parts:
        raise DomainMcpError("The exposed checkout metadata scan returned an unsafe path.")
    path = project / relative
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DomainMcpError("The exposed checkout changed during its boundary scan.") from exc
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
        raise DomainMcpError(
            "The exposed checkout contains a hardlinked regular file; "
            "replace hardlinks with independent files before enabling full access."
        )
    raise DomainMcpError(
        "The exposed checkout contains a socket, device, FIFO, or other "
        "unsupported filesystem entry."
    )


def verify_git_config_safe(project: Path) -> None:
    config_path = project / ".git" / "config"
    try:
        metadata = config_path.lstat()
    except OSError as exc:
        raise DomainMcpError("The checkout Git config is unavailable.") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or config_path.is_symlink()
        or metadata.st_nlink > 1
        or metadata.st_size > 1024 * 1024
    ):
        raise DomainMcpError("The checkout Git config is unsafe.")
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DomainMcpError("The checkout Git config could not be inspected safely.") from exc
    if re.search(r"(?im)^\s*\[\s*include(?:if)?\b", text):
        raise DomainMcpError("Git config include directives are not permitted in full-access mode.")
    if re.search(r"(?im)^\s*extraheader\s*=", text):
        raise DomainMcpError("Credential-bearing Git extra headers are not permitted.")
    if re.search(r"(?i)\bhttps?://[^\s/@]+@", text):
        raise DomainMcpError("Credential-bearing Git remote URLs are not permitted.")
    if re.search(
        r"(?im)^\s*(?:"
        r"fsmonitor|hookspath|worktree|worktreeconfig|sshcommand|"
        r"attributesfile|excludesfile|askpass|helper|command|textconv|"
        r"diffFilter|clean|smudge|process|driver"
        r")\s*=",
        text,
    ):
        raise DomainMcpError("Command-bearing or external-path Git config is not permitted.")
    alternates = project / ".git" / "objects" / "info" / "alternates"
    try:
        if alternates.exists() and alternates.read_bytes().strip():
            raise DomainMcpError("Git alternate object databases are not permitted.")
    except OSError as exc:
        raise DomainMcpError("Git alternate-object configuration could not be inspected.") from exc
    git_dir = project / ".git"
    git_walk_errors: list[OSError] = []
    for root, directories, files in os.walk(
        git_dir,
        topdown=True,
        followlinks=False,
        onerror=git_walk_errors.append,
    ):
        for name in [*directories, *files]:
            path = Path(root) / name
            try:
                if path.is_symlink():
                    raise DomainMcpError("Symlinks inside .git are not permitted.")
            except OSError as exc:
                raise DomainMcpError("Git metadata changed during its safety check.") from exc
    if git_walk_errors:
        raise DomainMcpError("Git metadata could not be scanned completely.")
    scanner = load_agent_mode_module()
    if scanner.content_secret_reason(config_path, max_bytes=1024 * 1024):
        raise DomainMcpError("The checkout Git config contains secret-looking material.")


def load_agent_mode_module() -> Any:
    global _AGENT_MODE_MODULE
    if _AGENT_MODE_MODULE is not None:
        return _AGENT_MODE_MODULE
    script = Path(__file__).resolve().with_name("agent_mode.py")
    script_dir = str(script.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("_advisor_domain_agent_mode", script)
    if spec is None or spec.loader is None:
        raise DomainMcpError("Could not load the shared advisor secret scanner.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    _AGENT_MODE_MODULE = module
    return module


def sensitive_mask_plan(project: Path) -> list[str]:
    scanner = load_agent_mode_module()
    candidates: list[Path] = []
    # This trusted original-checkout mode does not classify every file's
    # contents. Perform a bounded path-only pass so large datasets stay visible
    # without reading their bytes. Keep Git objects for read-only history, while
    # hiding local remotes/configuration and executable hooks.
    git_metadata_masks = {
        Path(".git/config"),
        Path(".git/config.worktree"),
        Path(".git/hooks"),
        Path(".codex-advisor"),
    }
    for relative in git_metadata_masks:
        if (project / relative).exists():
            candidates.append(relative)

    # Bulk research/runtime roots are deliberately exposed, not sanitized copies.
    # Do not enumerate their individual path names for secret-name masking. The
    # separate native boundary pass still checks the complete tree for mounts,
    # hardlinks, and special files without reading dataset or artifact contents.
    prune_expression: list[str] = ["("]
    for index, name in enumerate(FULL_ACCESS_BULK_PATH_SCAN_ROOTS):
        if index:
            prune_expression.append("-o")
        prune_expression.extend(["-path", str(project / name)])
    prune_expression.extend([")", "-prune", "-o"])
    records = run_find_bytes(
        project,
        [
            *prune_expression,
            "-mindepth",
            "1",
            "-printf",
            "%y\\0%P\\0",
        ],
        maximum_bytes=MAX_GIT_PATH_BYTES,
    ).split(b"\0")
    if records[-1:] != [b""]:
        raise DomainMcpError("The full-access path scan returned malformed metadata.")
    records.pop()
    if len(records) % 2:
        raise DomainMcpError("The full-access path scan returned malformed metadata.")
    path_scan_entries = len(records) // 2
    if path_scan_entries > MAX_EXPOSED_TREE_ENTRIES:
        raise DomainMcpError("The full-access path scan exceeded its entry limit.")

    for index in range(0, len(records), 2):
        entry_type = records[index].decode("ascii", errors="strict")
        relative = Path(os.fsdecode(records[index + 1]))
        if relative.is_absolute() or ".." in relative.parts:
            raise DomainMcpError("The full-access path scan returned an unsafe path.")
        if relative.parts[0] == ".git":
            continue
        if relative.name == ".git":
            candidates.append(relative)
            continue
        if not scanner.contains_sensitive_full_access_relative_marker(relative):
            continue
        path = project / relative
        if (
            entry_type == "f"
            and relative.name.lower() in SAFE_ENV_TEMPLATE_NAMES
            and not scanner.content_secret_reason(path, max_bytes=262_144)
        ):
            continue
        candidates.append(relative)

    collapsed: list[Path] = []
    collapsed_set: set[Path] = set()
    for relative in sorted(set(candidates), key=lambda value: (len(value.parts), str(value))):
        if relative in collapsed_set or any(
            parent in collapsed_set for parent in relative.parents
        ):
            continue
        collapsed.append(relative)
        collapsed_set.add(relative)
    if len(collapsed) > 4096:
        raise DomainMcpError("The full-access sensitive-path mask plan exceeded its limit.")
    return [str(path) for path in collapsed]


def ensure_mask_sources(state: Path) -> tuple[Path, Path]:
    masks = private_dir(state / "masks")
    empty_directory = private_dir(masks / "empty-directory")
    empty_file = masks / "empty-file"
    if empty_file.exists():
        metadata = empty_file.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or empty_file.is_symlink()
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise DomainMcpError("Sensitive-path mask placeholder is unsafe.")
    else:
        atomic_write(empty_file, "", mode=0o600)
    return empty_directory, empty_file


def verify_sensitive_mask_plan(config: dict[str, Any]) -> None:
    project = Path(config["projectDir"])
    expected = config.get("sensitivePathMasks")
    if not isinstance(expected, list) or any(not isinstance(path, str) for path in expected):
        raise DomainMcpError("Sensitive-path mask plan is missing; rerun prepare.")
    if sensitive_mask_plan(project) != expected:
        raise DomainMcpError(
            "Sensitive files changed after prepare; stop and rerun prepare before full access."
        )


def git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def run_git_bytes(
    project: Path,
    git_executable: Path,
    arguments: list[str],
    *,
    timeout: int,
    maximum_bytes: int = MAX_GIT_PATH_BYTES,
) -> bytes:
    completed = subprocess.run(
        [
            str(git_executable),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-c",
            "interactive.diffFilter=",
            "-C",
            str(project),
            *arguments,
        ],
        env=git_environment(),
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode:
        raise DomainMcpError("Full-access mode requires a valid Git checkout with a HEAD commit.")
    if len(completed.stdout) > maximum_bytes:
        raise DomainMcpError("Git checkout metadata exceeded the full-access safety limit.")
    return completed.stdout


def decode_git_paths(raw: bytes) -> set[Path]:
    paths: set[Path] = set()
    for value in raw.split(b"\0"):
        if not value:
            continue
        relative = Path(os.fsdecode(value))
        if relative.is_absolute() or relative in {Path("."), Path("")} or ".." in relative.parts:
            raise DomainMcpError("Git returned an unsafe checkout path.")
        paths.add(relative)
    return paths


def git_snapshot_inputs(
    project: Path,
    git_executable: Path,
) -> tuple[str, bytes, set[Path]]:
    status = run_git_bytes(
        project,
        git_executable,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout=30,
        maximum_bytes=16 * 1024 * 1024,
    )
    head_raw = run_git_bytes(
        project,
        git_executable,
        ["rev-parse", "--verify", "HEAD"],
        timeout=10,
        maximum_bytes=1024,
    )
    head = head_raw.decode("ascii", errors="strict").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{40,64}", head):
        raise DomainMcpError("Git HEAD is malformed.")

    tracked_state = run_git_bytes(
        project,
        git_executable,
        ["ls-files", "-v", "-z"],
        timeout=30,
    )
    for record in tracked_state.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise DomainMcpError("Git tracked-file metadata was malformed.")
        tag = chr(record[0])
        if tag == "S" or tag.islower():
            raise DomainMcpError(
                "Full-access mode rejects skip-worktree and assume-unchanged Git entries."
            )

    mutable_paths = decode_git_paths(
        run_git_bytes(
            project,
            git_executable,
            ["diff", "--name-only", "-z", "HEAD", "--"],
            timeout=30,
        )
    )
    mutable_paths.update(
        decode_git_paths(
            run_git_bytes(
                project,
                git_executable,
                ["ls-files", "--others", "--exclude-standard", "-z"],
                timeout=30,
            )
        )
    )
    # Ignored data/model trees are part of the live original-checkout mount, but
    # hashing every byte makes prepare unusable for large research repositories.
    # Boundary and secret scans still cover those paths; content pinning is kept
    # for tracked modifications, non-ignored untracked files, and Git config.
    mutable_paths.add(Path(".git/config"))
    return head, status, mutable_paths


def mutable_tree_fingerprint(
    project: Path,
    initial_paths: set[Path],
) -> tuple[str, int]:
    pending = set(initial_paths)
    expanded: set[Path] = set()
    while pending:
        relative = pending.pop()
        if relative in expanded:
            continue
        expanded.add(relative)
        if len(expanded) > MAX_EXPOSED_TREE_ENTRIES:
            raise DomainMcpError("The mutable checkout fingerprint exceeded its entry limit.")
        path = project / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DomainMcpError("A mutable checkout path could not be inspected.") from exc
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = list(path.iterdir())
            except OSError as exc:
                raise DomainMcpError("A mutable checkout directory could not be inspected.") from exc
            for child in children:
                child_relative = relative / child.name
                if child_relative.is_absolute() or ".." in child_relative.parts:
                    raise DomainMcpError("A mutable checkout directory contained an unsafe path.")
                pending.add(child_relative)

    digest = hashlib.sha256()
    records = 0
    for relative in sorted(expanded, key=str):
        path = project / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            record: dict[str, Any] = {"type": "missing"}
        except OSError as exc:
            raise DomainMcpError("A mutable checkout path changed during fingerprinting.") from exc
        else:
            if stat.S_ISREG(metadata.st_mode):
                record = stable_regular_file_record(path)
            elif stat.S_ISDIR(metadata.st_mode):
                record = {
                    "type": "directory",
                    "mode": stat.S_IMODE(metadata.st_mode),
                }
            elif stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise DomainMcpError(
                        "A mutable checkout symlink changed during fingerprinting."
                    ) from exc
                record = {
                    "type": "symlink",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "target": target,
                }
            else:
                raise DomainMcpError(
                    "A mutable checkout path is not a regular file, directory, or symlink."
                )
        encoded = json.dumps(
            {"path": str(relative), **record},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogateescape")
        digest.update(encoded)
        digest.update(b"\0")
        records += 1
    return digest.hexdigest(), records


def git_checkout_state(
    project: Path,
    git_executable: Path | None = None,
) -> dict[str, Any]:
    git_executable = (
        require_executable("git")
        if git_executable is None
        else git_executable.resolve(strict=True)
    )
    validate_runtime_executable(git_executable, project, "Git")
    verify_git_config_safe(project)
    first_head, first_status, first_paths = git_snapshot_inputs(project, git_executable)
    first_mutable, first_count = mutable_tree_fingerprint(project, first_paths)
    second_head, second_status, second_paths = git_snapshot_inputs(project, git_executable)
    second_mutable, second_count = mutable_tree_fingerprint(project, second_paths)
    if (
        first_head != second_head
        or first_status != second_status
        or first_paths != second_paths
        or first_mutable != second_mutable
        or first_count != second_count
    ):
        raise DomainMcpError("The checkout changed while its exact state was fingerprinted.")
    digest = hashlib.sha256(
        first_head.encode("ascii")
        + b"\0"
        + first_status
        + b"\0"
        + first_mutable.encode("ascii")
    ).hexdigest()
    return {
        "head": first_head,
        "dirty": bool(first_status),
        "fingerprint": digest,
        "mutableFingerprint": first_mutable,
        "mutableEntryCount": first_count,
    }


def verify_checkout_state(config: dict[str, Any]) -> None:
    current = git_checkout_state(
        Path(config["projectDir"]),
        Path(config["gitExecutable"]),
    )
    if (
        current["head"] != config.get("gitHead")
        or current["fingerprint"] != config.get("gitStateFingerprint")
        or current["mutableFingerprint"] != config.get("gitMutableFingerprint")
        or current["mutableEntryCount"] != config.get("gitMutableEntryCount")
    ):
        raise DomainMcpError(
            "The checkout changed after prepare; stop and rerun prepare before full access."
        )


def normalize_team_domain(value: str) -> str:
    candidate = value.strip().lower().removeprefix("https://").rstrip("/")
    if not candidate.endswith(".cloudflareaccess.com") or "/" in candidate:
        raise DomainMcpError(
            "Cloudflare Access team domain must look like your-team.cloudflareaccess.com."
        )
    return candidate


def validate_email(value: str) -> str:
    email = value.strip().lower()
    if (
        not email
        or email.count("@") != 1
        or any(character.isspace() for character in email)
        or "." not in email.split("@", 1)[1]
    ):
        raise DomainMcpError("A valid allowed email address is required.")
    return email


def validate_hostname(value: str) -> str:
    hostname = value.strip().lower().rstrip(".")
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DomainMcpError("The public hostname must use ASCII DNS labels.") from exc
    labels = hostname.split(".")
    if (
        len(labels) < 2
        or len(hostname) > 253
        or any(
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or any(not (character in "abcdefghijklmnopqrstuvwxyz0123456789-") for character in label)
            for label in labels
        )
    ):
        raise DomainMcpError("The public hostname is malformed.")
    return hostname


def bounded_integer(
    value: Any,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise DomainMcpError(f"{name} must be an integer between {minimum} and {maximum}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DomainMcpError(
            f"{name} must be an integer between {minimum} and {maximum}."
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise DomainMcpError(f"{name} must be an integer between {minimum} and {maximum}.")
    return parsed


def validate_account_id(value: str) -> str:
    account_id = value.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", account_id):
        raise DomainMcpError("Cloudflare account ID must be 32 hexadecimal characters.")
    return account_id


def validate_zone_id(value: str) -> str:
    zone_id = value.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", zone_id):
        raise DomainMcpError("Cloudflare zone ID must be 32 hexadecimal characters.")
    return zone_id


def validate_tunnel_id(value: str) -> str:
    try:
        tunnel_id = str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as exc:
        raise DomainMcpError("Cloudflare tunnel ID must be a UUID.") from exc
    return tunnel_id


def validate_redirect_uri(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise DomainMcpError("The ChatGPT OAuth redirect URI is malformed.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise DomainMcpError(
            "The redirect URI must be an approved HTTPS chatgpt.com callback."
        )
    approved_callbacks = {
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector/oauth/*",
    }
    if candidate not in approved_callbacks:
        raise DomainMcpError(
            "The redirect URI must be ChatGPT's legacy exact callback or the narrow "
            "https://chatgpt.com/connector/oauth/* callback family."
        )
    return candidate


def duration_seconds(value: Any, *, name: str) -> int:
    match = re.fullmatch(r"\s*(\d+)([smhd])\s*", str(value or ""), re.IGNORECASE)
    if not match:
        raise DomainMcpError(f"{name} must use a Cloudflare duration such as 15m or 24h.")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    return amount * multiplier


def ensure_secret(path: Path) -> None:
    if path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_mode & 0o077:
            raise DomainMcpError(f"Existing secret file is unsafe: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise DomainMcpError(f"Existing secret file is not owned by the current user: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 43:
            raise DomainMcpError(f"Existing secret file is malformed: {path}")
        return
    atomic_write(path, secrets.token_urlsafe(48) + "\n")


def read_private_token(path: Path) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DomainMcpError(f"Cloudflare API token file is missing: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise DomainMcpError("Cloudflare API token file must be owner-only mode 600.")
    token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise DomainMcpError("Cloudflare API token file is empty or malformed.")
    return token


def devspace_layout(executable: Path, node: Path) -> tuple[Path, Path]:
    node_root = node.parent.parent.resolve()
    devspace = executable.resolve()
    candidates = [devspace.parent] if devspace.name == "cli.js" else []
    candidates.extend(parent / "dist" for parent in devspace.parents)
    dist = next(
        (
            candidate
            for candidate in candidates
            if (candidate / "cli.js").is_file()
            and (candidate / "config.js").is_file()
            and (candidate / "server.js").is_file()
        ),
        None,
    )
    if dist is None:
        raise DomainMcpError(f"Could not locate the DevSpace package from {executable}.")
    try:
        dist.relative_to(node_root)
    except ValueError as exc:
        raise DomainMcpError("DevSpace and Node must be installed under the same versioned root.") from exc
    return node_root, dist.resolve()


def run_patch(script: Path, executable: Path, *, check: bool) -> None:
    command = [sys.executable, str(script), "--executable", str(executable)]
    if check:
        command.append("--check")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise DomainMcpError(f"{script.name} failed: {detail}")


def patch_devspace(executable: Path, *, check: bool = False) -> None:
    scripts = Path(__file__).resolve().parent
    run_patch(scripts / "devspace_readonly_patch.py", executable, check=check)
    run_patch(scripts / "devspace_secure_origin_patch.py", executable, check=check)


def access_complete(config: dict[str, Any]) -> bool:
    return bool(
        config.get("accessIssuer")
        and config.get("accessAudience")
        and config.get("allowedEmails")
    )


def config_paths(config_path: Path) -> dict[str, Path]:
    config_dir = config_path.parent.resolve()
    origin_runtime = DEFAULT_RUNTIME_DIR / ORIGIN_RUNTIME_NAME
    gateway_runtime = DEFAULT_RUNTIME_DIR / GATEWAY_RUNTIME_NAME
    return {
        "config": config_path.resolve(),
        "secret": config_dir / "origin-secret",
        "pinned": config_dir / "pinned-root",
        "gateway_config": config_dir / "gateway-runtime.json",
        "state": DEFAULT_STATE_DIR,
        "runtime": DEFAULT_RUNTIME_DIR,
        "origin_runtime": origin_runtime,
        "gateway_runtime": gateway_runtime,
        "socket": origin_runtime / "devspace.sock",
        "gateway_socket": gateway_runtime / PUBLIC_GATEWAY_SOCKET_NAME,
    }


def nvidia_device_plan(enabled: bool) -> list[dict[str, Any]]:
    if not enabled:
        return []
    candidates = [
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia-uvm"),
        Path("/dev/nvidia-uvm-tools"),
        Path("/dev/nvidia-modeset"),
        *sorted(Path("/dev").glob("nvidia[0-9]*")),
    ]
    plan: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISCHR(metadata.st_mode)
            or not NVIDIA_DEVICE_PATTERN.fullmatch(str(path))
            or not os.access(path, os.R_OK | os.W_OK)
        ):
            raise DomainMcpError(f"Unsafe or inaccessible NVIDIA device: {path}")
        plan.append(
            {
                "path": str(path),
                "major": os.major(metadata.st_rdev),
                "minor": os.minor(metadata.st_rdev),
            }
        )
    paths = {entry["path"] for entry in plan}
    if (
        "/dev/nvidiactl" not in paths
        or "/dev/nvidia-uvm" not in paths
        or not any(re.fullmatch(r"/dev/nvidia[0-9]+", path) for path in paths)
    ):
        raise DomainMcpError(
            "NVIDIA mode requires nvidiactl, nvidia-uvm, and at least one GPU device."
        )
    return sorted(plan, key=lambda entry: entry["path"])


def verify_nvidia_device_plan(config: dict[str, Any]) -> list[str]:
    mode = config.get("gpuMode")
    plan = config.get("nvidiaDevices")
    if mode not in {"none", "nvidia"} or not isinstance(plan, list):
        raise DomainMcpError("GPU device policy is malformed.")
    if mode == "none":
        if plan:
            raise DomainMcpError("Disabled GPU mode cannot contain device bindings.")
        return []
    expected = nvidia_device_plan(True)
    if plan != expected:
        raise DomainMcpError("Pinned NVIDIA device plan changed; rerun prepare.")
    return [str(entry["path"]) for entry in expected]


def nvidia_bwrap_arguments(config: dict[str, Any]) -> list[str]:
    arguments: list[str] = []
    for path in verify_nvidia_device_plan(config):
        arguments.extend(["--dev-bind", path, path])
    return arguments


def prepare_config(args: argparse.Namespace) -> dict[str, Any]:
    if (
        service_active(ORIGIN_UNIT)
        or service_active(GATEWAY_UNIT)
        or service_active(EXPIRY_TIMER_UNIT)
    ):
        raise DomainMcpError(
            "Stop the current domain MCP services before changing the exposed project."
        )
    config_path = Path(args.config).expanduser().resolve()
    paths = config_paths(config_path)
    project, metadata = resolve_project(args.project_dir)
    python = Path(sys.executable).resolve(strict=True)
    validate_runtime_executable(python, project, "Python")
    verify_exposed_tree_boundary(project)
    git = require_executable("git")
    validate_runtime_executable(git, project, "Git")
    git_state = git_checkout_state(project, git)
    if git_state["dirty"] and not args.allow_dirty_checkout:
        raise DomainMcpError(
            "The original checkout is dirty. Commit/stash it, use a sanitized workspace, or "
            "rerun prepare with --allow-dirty-checkout for this exact reviewed state."
        )
    try:
        paths["config"].relative_to(project)
    except ValueError:
        pass
    else:
        raise DomainMcpError("Domain MCP config and credentials must live outside the exposed project.")
    bwrap = require_executable(args.bwrap)
    node = require_executable(args.node)
    devspace = require_executable(args.devspace)
    validate_runtime_executable(bwrap, project, "Bubblewrap")
    validate_runtime_executable(node, project, "Node")
    node_root, devspace_dist = devspace_layout(devspace, node)
    patch_devspace(devspace)
    devspace_runtime_files = [
        path
        for path in devspace_dist.rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    package_json = devspace_dist.parent / "package.json"
    if package_json.is_file() and not package_json.is_symlink():
        devspace_runtime_files.append(package_json)
    for runtime_file in (devspace, *devspace_runtime_files):
        harden_user_owned_runtime_file(runtime_file.resolve(strict=True))
    validate_runtime_executable(devspace, project, "DevSpace")

    private_dir(paths["config"].parent)
    private_dir(paths["state"])
    require_system_runtime_base(paths["runtime"])
    private_dir(paths["origin_runtime"])
    private_dir(paths["gateway_runtime"])
    sensitive_masks = sensitive_mask_plan(project)
    ensure_mask_sources(paths["state"])
    ensure_secret(paths["secret"])
    atomic_write(paths["pinned"], "/workspace\n")

    existing: dict[str, Any] = {}
    if paths["config"].exists():
        existing = read_private_json(paths["config"])
    hostname = validate_hostname(args.hostname or str(existing.get("publicHostname") or DEFAULT_HOSTNAME))
    team_domain = args.team_domain or ""
    audience = args.audience.strip() if args.audience else ""
    email = args.email or ""
    if not team_domain and existing.get("accessIssuer"):
        team_domain = str(existing["accessIssuer"]).removeprefix("https://")
    if not audience:
        audience = str(existing.get("accessAudience") or "")
    if not email and existing.get("allowedEmails"):
        email = str(existing["allowedEmails"][0])

    issuer = f"https://{normalize_team_domain(team_domain)}" if team_domain else ""
    allowed_emails = [validate_email(email)] if email else []
    if bool(issuer) + bool(audience) + bool(allowed_emails) not in {0, 3}:
        raise DomainMcpError(
            "Configure team domain, Access audience, and allowed email together, or omit all three."
        )
    if audience and (len(audience) < 8 or len(audience) > 256):
        raise DomainMcpError("Cloudflare Access application audience is malformed.")

    session_minutes = bounded_integer(
        args.session_minutes,
        name="session minutes",
        minimum=MIN_SESSION_MINUTES,
        maximum=MAX_SESSION_MINUTES,
    )
    origin_memory_mb = bounded_integer(
        args.origin_memory_mb,
        name="origin memory limit",
        minimum=256,
        maximum=16 * 1024,
    )
    gateway_memory_mb = bounded_integer(
        args.gateway_memory_mb,
        name="gateway memory limit",
        minimum=64,
        maximum=2048,
    )
    origin_cpu_percent = bounded_integer(
        args.origin_cpu_percent,
        name="origin CPU quota",
        minimum=25,
        maximum=800,
    )
    gateway_cpu_percent = bounded_integer(
        args.gateway_cpu_percent,
        name="gateway CPU quota",
        minimum=10,
        maximum=400,
    )
    max_file_mb = bounded_integer(
        args.max_file_mb,
        name="per-file size limit",
        minimum=16,
        maximum=4096,
    )
    min_free_space_mb = bounded_integer(
        args.min_free_space_mb,
        name="minimum free-space reserve",
        minimum=1024,
        maximum=1024 * 1024,
    )
    min_free_inodes = bounded_integer(
        args.min_free_inodes,
        name="minimum free-inode reserve",
        minimum=1000,
        maximum=10_000_000,
    )
    max_concurrent = bounded_integer(
        args.max_concurrent,
        name="maximum concurrency",
        minimum=1,
        maximum=MAX_CONCURRENT_LIMIT,
    )
    max_body_bytes = bounded_integer(
        args.max_body_bytes,
        name="maximum request body",
        minimum=64 * 1024,
        maximum=64 * 1024 * 1024,
    )
    nvidia_devices = nvidia_device_plan(bool(args.enable_nvidia))

    scripts = Path(__file__).resolve().parent
    try:
        scripts.relative_to(project)
    except ValueError:
        pass
    else:
        raise DomainMcpError(
            "Run the installed external-advisor skill, not a manager script inside the exposed project."
        )
    manager = Path(__file__).resolve(strict=True)
    agent_mode_script = (scripts / "agent_mode.py").resolve(strict=True)
    advisor_concurrency = (scripts / "advisor_concurrency.py").resolve(strict=True)
    advisor_safety = (scripts / "advisor_safety.py").resolve(strict=True)
    readonly_patch = (scripts / "devspace_readonly_patch.py").resolve(strict=True)
    secure_origin_patch = (scripts / "devspace_secure_origin_patch.py").resolve(strict=True)
    gateway_script = (scripts / "cloudflare_access_gateway.mjs").resolve(strict=True)
    secure_server = (scripts / "devspace_secure_server.mjs").resolve(strict=True)
    shell_sandbox = (scripts / "devspace_shell_sandbox.py").resolve(strict=True)
    for name, path in (
        ("Domain MCP manager", manager),
        ("Agent-mode scanner", agent_mode_script),
        ("Advisor concurrency helper", advisor_concurrency),
        ("Advisor safety helper", advisor_safety),
        ("DevSpace read-only patch", readonly_patch),
        ("DevSpace secure-origin patch", secure_origin_patch),
        ("Cloudflare Access gateway", gateway_script),
        ("DevSpace secure server", secure_server),
        ("DevSpace shell sandbox", shell_sandbox),
    ):
        harden_user_owned_runtime_file(path)
        validate_runtime_executable(path, project, name)
    payload: dict[str, Any] = {
        "schemaVersion": CONFIG_SCHEMA,
        "projectDir": str(project),
        "projectDevice": metadata.st_dev,
        "projectInode": metadata.st_ino,
        "gitHead": git_state["head"],
        "gitStateFingerprint": git_state["fingerprint"],
        "gitMutableFingerprint": git_state["mutableFingerprint"],
        "gitMutableEntryCount": git_state["mutableEntryCount"],
        "dirtyCheckoutApproved": bool(git_state["dirty"] and args.allow_dirty_checkout),
        "publicHostname": hostname,
        "accessIssuer": issuer,
        "accessAudience": audience,
        "allowedEmails": allowed_emails,
        "gatewaySocket": str(paths["gateway_socket"]),
        "originSocket": str(paths["socket"]),
        "upstreamSecretFile": str(paths["secret"]),
        "gatewayRuntimeConfig": str(paths["gateway_config"]),
        "maxConcurrent": max_concurrent,
        "maxBodyBytes": max_body_bytes,
        "clockSkewSeconds": 60,
        "stateDir": str(paths["state"]),
        "runtimeDir": str(paths["runtime"]),
        "originRuntimeDir": str(paths["origin_runtime"]),
        "gatewayRuntimeDir": str(paths["gateway_runtime"]),
        "pinnedRootFile": str(paths["pinned"]),
        "pythonExecutable": str(python),
        "gitExecutable": str(git),
        "bwrapPath": str(bwrap),
        "nodeRoot": str(node_root),
        "devspaceDist": str(devspace_dist),
        "devspaceExecutable": str(devspace),
        "agentModeScript": str(agent_mode_script),
        "advisorConcurrencyScript": str(advisor_concurrency),
        "advisorSafetyScript": str(advisor_safety),
        "readonlyPatchScript": str(readonly_patch),
        "secureOriginPatchScript": str(secure_origin_patch),
        "gatewayScript": str(gateway_script),
        "secureServerScript": str(secure_server),
        "shellSandboxScript": str(shell_sandbox),
        "managerScript": str(manager),
        "networkPolicy": "isolated",
        "toolMode": "full",
        "fullCompute": bool(args.full_compute),
        "gpuMode": "nvidia" if args.enable_nvidia else "none",
        "nvidiaDevices": nvidia_devices,
        "sessionDurationMinutes": session_minutes,
        "originMemoryMaxBytes": origin_memory_mb * 1024 * 1024,
        "gatewayMemoryMaxBytes": gateway_memory_mb * 1024 * 1024,
        "originCpuQuotaPercent": origin_cpu_percent,
        "gatewayCpuQuotaPercent": gateway_cpu_percent,
        "maxFileSizeBytes": max_file_mb * 1024 * 1024,
        "minFreeSpaceBytes": min_free_space_mb * 1024 * 1024,
        "minFreeInodes": min_free_inodes,
        "sensitivePathMasks": sensitive_masks,
    }
    if not filesystem_capacity_healthy(
        [project, paths["state"]],
        minimum_free_bytes=payload["minFreeSpaceBytes"],
        minimum_free_inodes=payload["minFreeInodes"],
    ):
        raise DomainMcpError(
            "The project or state filesystem is already below the configured free-space reserve."
        )
    payload["runtimeIntegrity"] = runtime_integrity_manifest(payload)
    existing_hardening = existing.get("cloudflareHardening")
    if (
        isinstance(existing_hardening, dict)
        and existing.get("publicHostname") == hostname
        and existing.get("accessIssuer") == issuer
        and existing.get("accessAudience") == audience
        and existing.get("allowedEmails") == allowed_emails
        and existing.get("projectDevice") == metadata.st_dev
        and existing.get("projectInode") == metadata.st_ino
        and existing.get("gatewaySocket") == str(paths["gateway_socket"])
    ):
        payload["cloudflareHardening"] = existing_hardening
    atomic_json(paths["config"], payload)
    write_gateway_runtime_config(payload)
    install_units(payload, paths["config"])
    return payload


def gateway_runtime_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "accessIssuer": config["accessIssuer"],
        "accessAudience": config["accessAudience"],
        "allowedEmails": config["allowedEmails"],
        "publicHostname": config["publicHostname"],
        "originSocket": "/run/advisor-origin/devspace.sock",
        "upstreamSecretFile": "/run/advisor-config/origin-secret",
        "gatewaySocket": f"/run/advisor-gateway/{PUBLIC_GATEWAY_SOCKET_NAME}",
        "maxBodyBytes": config["maxBodyBytes"],
        "maxConcurrent": config["maxConcurrent"],
        "clockSkewSeconds": config["clockSkewSeconds"],
    }


def write_gateway_runtime_config(config: dict[str, Any]) -> None:
    atomic_json(Path(config["gatewayRuntimeConfig"]), gateway_runtime_payload(config))


def systemd_quote(value: str) -> str:
    if any(character in value for character in "\r\n\0"):
        raise DomainMcpError("Systemd values must not contain control characters.")
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def origin_unit(config: dict[str, Any], config_path: Path) -> str:
    command = " ".join(
        systemd_quote(value)
        for value in (
            str(config["pythonExecutable"]),
            str(config["managerScript"]),
            "run-origin",
            "--config",
            str(config_path),
        )
    )
    if config["fullCompute"]:
        compute_limits = """MemoryMax=infinity
MemorySwapMax=infinity
TasksMax=infinity
LimitNOFILE=infinity
LimitFSIZE=infinity"""
    else:
        compute_limits = f"""MemoryMax={int(config["originMemoryMaxBytes"])}
MemorySwapMax=0
CPUQuota={int(config["originCpuQuotaPercent"])}%
TasksMax=128
LimitNOFILE=4096
LimitFSIZE={int(config["maxFileSizeBytes"])}"""
    return f"""[Unit]
Description=Advisor MCP sandboxed DevSpace origin
After=default.target
OnFailure={EXPIRY_SERVICE_UNIT}

[Service]
Type=simple
ExecStart={command}
Restart=no
RuntimeMaxSec={int(config["sessionDurationMinutes"]) * 60}
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
{compute_limits}
LimitCORE=0
OOMPolicy=stop
"""


def gateway_unit(config: dict[str, Any], config_path: Path) -> str:
    command = " ".join(
        systemd_quote(value)
        for value in (
            str(config["pythonExecutable"]),
            str(config["managerScript"]),
            "run-gateway",
            "--config",
            str(config_path),
        )
    )
    return f"""[Unit]
Description=Advisor MCP Cloudflare Access gateway
After=network-online.target {ORIGIN_UNIT} {EXPIRY_TIMER_UNIT}
Wants=network-online.target
Requires={ORIGIN_UNIT} {EXPIRY_TIMER_UNIT}
BindsTo={ORIGIN_UNIT} {EXPIRY_TIMER_UNIT}
OnFailure={EXPIRY_SERVICE_UNIT}

[Service]
Type=simple
ExecStart={command}
Restart=no
RuntimeMaxSec={int(config["sessionDurationMinutes"]) * 60}
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
MemoryMax={int(config["gatewayMemoryMaxBytes"])}
MemorySwapMax=0
CPUQuota={int(config["gatewayCpuQuotaPercent"])}%
TasksMax=32
LimitNOFILE=4096
LimitFSIZE=16777216
LimitCORE=0
OOMPolicy=stop
"""


def expiry_service_unit(config: dict[str, Any], config_path: Path) -> str:
    command = " ".join(
        systemd_quote(value)
        for value in (
            str(config["pythonExecutable"]),
            str(config["managerScript"]),
            "expire",
            "--config",
            str(config_path),
        )
    )
    return f"""[Unit]
Description=Expire the temporary Advisor MCP full-access window

[Service]
Type=oneshot
ExecStart={command}
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX
MemoryMax=67108864
MemorySwapMax=0
CPUQuota=25%
TasksMax=16
LimitNOFILE=256
LimitFSIZE=1048576
LimitCORE=0
"""


def expiry_timer_unit(config: dict[str, Any]) -> str:
    return f"""[Unit]
Description=Automatic shutdown timer for the Advisor MCP full-access window

[Timer]
OnActiveSec={int(config["sessionDurationMinutes"])}m
AccuracySec=1s
Unit={EXPIRY_SERVICE_UNIT}

[Install]
WantedBy=timers.target
"""


def install_units(config: dict[str, Any], config_path: Path) -> None:
    private_dir(UNIT_DIR)
    atomic_write(UNIT_DIR / ORIGIN_UNIT, origin_unit(config, config_path), mode=0o644)
    atomic_write(UNIT_DIR / GATEWAY_UNIT, gateway_unit(config, config_path), mode=0o644)
    atomic_write(
        UNIT_DIR / EXPIRY_SERVICE_UNIT,
        expiry_service_unit(config, config_path),
        mode=0o644,
    )
    atomic_write(
        UNIT_DIR / EXPIRY_TIMER_UNIT,
        expiry_timer_unit(config),
        mode=0o644,
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def verify_config_runtime(config: dict[str, Any]) -> tuple[Path, os.stat_result]:
    if config.get("schemaVersion") != CONFIG_SCHEMA:
        raise DomainMcpError(
            "Unsupported domain MCP config schema. Stop the connector, rerun prepare, "
            "install the local tunnel identity marker, and rerun audit-cloudflare."
        )
    project, metadata = resolve_project(str(config.get("projectDir") or ""))
    if metadata.st_dev != config.get("projectDevice") or metadata.st_ino != config.get("projectInode"):
        raise DomainMcpError("The configured project directory was replaced; rerun prepare.")
    if config.get("networkPolicy") != "isolated" or config.get("toolMode") != "full":
        raise DomainMcpError("Unsafe or unsupported domain MCP runtime policy.")
    if not isinstance(config.get("fullCompute"), bool):
        raise DomainMcpError("Compute resource policy is malformed.")
    if (
        not re.fullmatch(r"[a-f0-9]{40,64}", str(config.get("gitHead") or ""))
        or not re.fullmatch(r"[a-f0-9]{64}", str(config.get("gitStateFingerprint") or ""))
        or not re.fullmatch(r"[a-f0-9]{64}", str(config.get("gitMutableFingerprint") or ""))
        or isinstance(config.get("gitMutableEntryCount"), bool)
        or not isinstance(config.get("gitMutableEntryCount"), int)
        or not 1 <= int(config["gitMutableEntryCount"]) <= MAX_EXPOSED_TREE_ENTRIES
        or not isinstance(config.get("dirtyCheckoutApproved"), bool)
    ):
        raise DomainMcpError("Pinned Git checkout state is malformed.")
    bounded_integer(
        config.get("sessionDurationMinutes"),
        name="session minutes",
        minimum=MIN_SESSION_MINUTES,
        maximum=MAX_SESSION_MINUTES,
    )
    bounded_integer(
        config.get("originMemoryMaxBytes"),
        name="origin memory limit",
        minimum=256 * 1024 * 1024,
        maximum=16 * 1024 * 1024 * 1024,
    )
    bounded_integer(
        config.get("gatewayMemoryMaxBytes"),
        name="gateway memory limit",
        minimum=64 * 1024 * 1024,
        maximum=2 * 1024 * 1024 * 1024,
    )
    bounded_integer(
        config.get("originCpuQuotaPercent"),
        name="origin CPU quota",
        minimum=25,
        maximum=800,
    )
    bounded_integer(
        config.get("gatewayCpuQuotaPercent"),
        name="gateway CPU quota",
        minimum=10,
        maximum=400,
    )
    bounded_integer(
        config.get("maxFileSizeBytes"),
        name="per-file size limit",
        minimum=16 * 1024 * 1024,
        maximum=4 * 1024 * 1024 * 1024,
    )
    bounded_integer(
        config.get("maxConcurrent"),
        name="maximum concurrency",
        minimum=1,
        maximum=MAX_CONCURRENT_LIMIT,
    )
    bounded_integer(
        config.get("minFreeSpaceBytes"),
        name="minimum free-space reserve",
        minimum=1024 * 1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    bounded_integer(
        config.get("minFreeInodes"),
        name="minimum free-inode reserve",
        minimum=1000,
        maximum=10_000_000,
    )
    verify_nvidia_device_plan(config)
    sensitive_masks = config.get("sensitivePathMasks")
    if (
        not isinstance(sensitive_masks, list)
        or len(sensitive_masks) > 4096
        or any(
            not isinstance(value, str)
            or not value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
            for value in sensitive_masks
        )
    ):
        raise DomainMcpError("Sensitive-path mask plan is malformed.")
    runtime = Path(str(config.get("runtimeDir") or ""))
    origin_runtime = Path(str(config.get("originRuntimeDir") or ""))
    gateway_runtime = Path(str(config.get("gatewayRuntimeDir") or ""))
    gateway_socket = Path(str(config.get("gatewaySocket") or ""))
    origin_socket = Path(str(config.get("originSocket") or ""))
    runtime_directories = (runtime, origin_runtime, gateway_runtime)
    try:
        runtime_metadata = [path.lstat() for path in runtime_directories]
    except OSError as exc:
        raise DomainMcpError("Domain MCP runtime directories are unavailable.") from exc
    if any(
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        for path, metadata in zip(runtime_directories, runtime_metadata, strict=True)
    ) or (
        origin_runtime != runtime / ORIGIN_RUNTIME_NAME
        or gateway_runtime != runtime / GATEWAY_RUNTIME_NAME
        or gateway_socket != gateway_runtime / PUBLIC_GATEWAY_SOCKET_NAME
        or origin_socket != origin_runtime / "devspace.sock"
    ):
        raise DomainMcpError("Domain MCP runtime and socket paths are not the pinned private paths.")
    for key in (
        "pythonExecutable",
        "gitExecutable",
        "bwrapPath",
        "nodeRoot",
        "devspaceDist",
        "devspaceExecutable",
        "managerScript",
        "agentModeScript",
        "advisorConcurrencyScript",
        "advisorSafetyScript",
        "readonlyPatchScript",
        "secureOriginPatchScript",
        "gatewayScript",
        "secureServerScript",
        "shellSandboxScript",
        "pinnedRootFile",
        "upstreamSecretFile",
        "gatewayRuntimeConfig",
    ):
        path = Path(str(config.get(key) or ""))
        if not path.is_absolute() or not path.exists():
            raise DomainMcpError(f"Configured runtime path is unavailable: {key}")
    if read_private_json(Path(config["gatewayRuntimeConfig"])) != gateway_runtime_payload(config):
        raise DomainMcpError("Pinned gateway runtime config changed; rerun prepare.")
    for name, path in (
        ("Python", Path(config["pythonExecutable"]).resolve()),
        ("Git", Path(config["gitExecutable"]).resolve()),
        ("Bubblewrap", Path(config["bwrapPath"]).resolve()),
        ("Node", (Path(config["nodeRoot"]) / "bin" / "node").resolve()),
        ("DevSpace", Path(config["devspaceExecutable"]).resolve()),
        ("Domain MCP manager", Path(config["managerScript"]).resolve()),
        ("Agent-mode scanner", Path(config["agentModeScript"]).resolve()),
        (
            "Advisor concurrency helper",
            Path(config["advisorConcurrencyScript"]).resolve(),
        ),
        ("Advisor safety helper", Path(config["advisorSafetyScript"]).resolve()),
        ("DevSpace read-only patch", Path(config["readonlyPatchScript"]).resolve()),
        (
            "DevSpace secure-origin patch",
            Path(config["secureOriginPatchScript"]).resolve(),
        ),
        ("Cloudflare Access gateway", Path(config["gatewayScript"]).resolve()),
        ("DevSpace secure server", Path(config["secureServerScript"]).resolve()),
        ("DevSpace shell sandbox", Path(config["shellSandboxScript"]).resolve()),
    ):
        validate_runtime_executable(path, project, name)
    expected_integrity = config.get("runtimeIntegrity")
    if (
        not isinstance(expected_integrity, dict)
        or expected_integrity != runtime_integrity_manifest(config)
    ):
        raise DomainMcpError("Pinned runtime integrity changed; rerun prepare before starting.")
    verify_exposed_tree_boundary(project)
    verify_git_config_safe(project)
    return project, metadata


def sandbox_etc_arguments() -> list[str]:
    arguments = ["--dir", "/etc"]
    for source in (
        "/etc/alternatives",
        "/etc/ssl",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/nsswitch.conf",
        "/etc/os-release",
        "/etc/hosts",
        "/etc/resolv.conf",
        "/etc/passwd",
        "/etc/group",
    ):
        if Path(source).exists():
            arguments.extend(["--ro-bind", source, source])
    return arguments


def bwrap_command(config: dict[str, Any], command: list[str] | None = None) -> list[str]:
    project, _ = verify_config_runtime(config)
    verify_exposed_tree_boundary(project)
    origin_runtime = private_dir(Path(config["originRuntimeDir"]))
    state = private_dir(Path(config["stateDir"]))
    node_root = Path(config["nodeRoot"]).resolve()
    devspace_dist = Path(config["devspaceDist"]).resolve()
    dist_inside = Path("/opt/node") / devspace_dist.relative_to(node_root)
    socket_inside = Path("/run/advisor-origin/devspace.sock")
    secure_server = Path(config["secureServerScript"]).resolve()
    shell_sandbox = Path(config["shellSandboxScript"]).resolve()
    selected_command = command or [
        "/opt/node/bin/node",
        "/opt/advisor/devspace_secure_server.mjs",
    ]
    empty_mask_directory, empty_mask_file = ensure_mask_sources(state)
    arguments = [
        str(config["bwrapPath"]),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--dir",
        "/opt",
        "--ro-bind",
        str(node_root),
        "/opt/node",
        "--dir",
        "/opt/advisor",
        "--ro-bind",
        str(secure_server),
        "/opt/advisor/devspace_secure_server.mjs",
        "--ro-bind",
        str(shell_sandbox),
        "/opt/advisor/devspace_shell_sandbox.py",
        "--dir",
        "/home",
        "--dir",
        "/home/devspace",
        "--dir",
        "/run",
        "--bind",
        str(origin_runtime),
        "/run/advisor-origin",
        "--ro-bind",
        str(Path(config["pinnedRootFile"]).resolve()),
        "/run/advisor-pinned-root",
        "--bind",
        str(state),
        "/state",
        "--bind",
        str(project),
        "/workspace",
        "--ro-bind",
        str(project / ".git"),
        "/workspace/.git",
    ]
    for relative_value in config["sensitivePathMasks"]:
        relative = Path(relative_value)
        source = project / relative
        metadata = source.lstat()
        mask = empty_mask_directory if stat.S_ISDIR(metadata.st_mode) else empty_mask_file
        arguments.extend(["--ro-bind", str(mask), str(Path("/workspace") / relative)])
    arguments.extend(["--proc", "/proc", "--dev", "/dev"])
    arguments.extend(nvidia_bwrap_arguments(config))
    arguments.extend(["--tmpfs", "/tmp"])
    arguments.extend(sandbox_etc_arguments())
    environment = {
        "HOME": "/home/devspace",
        "USER": "devspace",
        "LOGNAME": "devspace",
        "PATH": "/opt/node/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "HOST": "127.0.0.1",
        "PORT": "7677",
        "DEVSPACE_DIST_DIR": str(dist_inside),
        "DEVSPACE_UNIX_SOCKET": str(socket_inside),
        "DEVSPACE_PUBLIC_BASE_URL": f"https://{config['publicHostname']}",
        "DEVSPACE_ALLOWED_ROOTS": "/workspace",
        "DEVSPACE_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "DEVSPACE_TOOL_MODE": "full",
        "DEVSPACE_PINNED_EXACT_ROOT_FILE": "/run/advisor-pinned-root",
        "DEVSPACE_TRUSTED_PROXY_AUTH_FILE": (
            f"/run/advisor-origin/{RUNTIME_SECRET_NAME}"
        ),
        "DEVSPACE_SHELL_SANDBOX": "/opt/advisor/devspace_shell_sandbox.py",
        "DEVSPACE_SHELL_MAX_SECONDS": str(
            int(config["sessionDurationMinutes"]) * 60
        ),
        "DEVSPACE_DISABLE_SYNC_SHELL": "true",
        "DEVSPACE_PROCESS_MAX_ACTIVE": str(int(config["maxConcurrent"])),
        "ADVISOR_PINNED_NVIDIA_DEVICES": ":".join(
            str(entry["path"]) for entry in config["nvidiaDevices"]
        ),
        "DEVSPACE_OAUTH_OWNER_TOKEN": "disabled-in-trusted-proxy-origin",
        "DEVSPACE_CONFIG_DIR": "/state/config",
        "DEVSPACE_STATE_DIR": "/state/devspace",
        "DEVSPACE_WORKTREE_ROOT": "/state/worktrees",
        "DEVSPACE_AGENT_DIR": "/state/agent",
        "DEVSPACE_SKILLS": "false",
        "DEVSPACE_SKILL_PATHS": "",
        "DEVSPACE_SUBAGENTS": "false",
        "DEVSPACE_WIDGETS": "off",
        "DEVSPACE_LOG_LEVEL": "warn",
        "DEVSPACE_LOG_REQUESTS": "false",
        "DEVSPACE_LOG_ASSETS": "false",
        "DEVSPACE_LOG_TOOL_CALLS": "false",
        "DEVSPACE_LOG_SHELL_COMMANDS": "false",
        "DEVSPACE_TRUST_PROXY": "false",
    }
    for name, value in environment.items():
        arguments.extend(["--setenv", name, value])
    arguments.extend(["--hostname", "advisor-mcp", "--chdir", "/workspace", "--"])
    arguments.extend(selected_command)
    return arguments


def stage_runtime_secret(config: dict[str, Any]) -> Path:
    verify_config_runtime(config)
    source = Path(config["upstreamSecretFile"])
    ensure_secret(source)
    value = source.read_text(encoding="utf-8")
    runtime = private_dir(Path(config["originRuntimeDir"]))
    destination = runtime / RUNTIME_SECRET_NAME
    atomic_write(destination, value, mode=0o600)
    return destination


def gateway_bwrap_command(config: dict[str, Any]) -> list[str]:
    verify_config_runtime(config)
    origin_runtime = private_dir(Path(config["originRuntimeDir"]))
    gateway_runtime = private_dir(Path(config["gatewayRuntimeDir"]))
    node_root = Path(config["nodeRoot"]).resolve()
    gateway = Path(config["gatewayScript"]).resolve()
    gateway_config = Path(config["gatewayRuntimeConfig"]).resolve()
    secret = Path(config["upstreamSecretFile"]).resolve()
    arguments = [
        str(config["bwrapPath"]),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--dir",
        "/opt",
        "--ro-bind",
        str(node_root),
        "/opt/node",
        "--dir",
        "/opt/advisor",
        "--ro-bind",
        str(gateway),
        "/opt/advisor/cloudflare_access_gateway.mjs",
        "--dir",
        "/home",
        "--dir",
        "/home/gateway",
        "--dir",
        "/run",
        "--bind",
        str(gateway_runtime),
        "/run/advisor-gateway",
        "--ro-bind",
        str(origin_runtime),
        "/run/advisor-origin",
        "--dir",
        "/run/advisor-config",
        "--ro-bind",
        str(gateway_config),
        "/run/advisor-config/config.json",
        "--ro-bind",
        str(secret),
        "/run/advisor-config/origin-secret",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    arguments.extend(sandbox_etc_arguments())
    for name, value in (
        ("HOME", "/home/gateway"),
        ("USER", "gateway"),
        ("LOGNAME", "gateway"),
        ("PATH", "/opt/node/bin:/usr/bin:/bin"),
        ("LANG", "C.UTF-8"),
        ("LC_ALL", "C.UTF-8"),
        ("NO_COLOR", "1"),
    ):
        arguments.extend(["--setenv", name, value])
    arguments.extend(
        [
            "--hostname",
            "advisor-gateway",
            "--chdir",
            "/home/gateway",
            "--",
            "/opt/node/bin/node",
            "/opt/advisor/cloudflare_access_gateway.mjs",
            "--config",
            "/run/advisor-config/config.json",
        ]
    )
    return arguments


def run_origin(config_path: Path) -> int:
    config = read_private_json(config_path)
    verify_config_runtime(config)
    patch_devspace(Path(config["devspaceExecutable"]), check=True)
    verify_checkout_state(config)
    verify_sensitive_mask_plan(config)
    stage_runtime_secret(config)
    command = bwrap_command(config)
    project = Path(config["projectDir"])
    state = Path(config["stateDir"])
    process: subprocess.Popen[Any] | None = None

    def forward_signal(signum: int, _frame: Any) -> None:
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signum)
            except OSError:
                pass

    previous_term = signal.signal(signal.SIGTERM, forward_signal)
    previous_int = signal.signal(signal.SIGINT, forward_signal)
    try:
        process = subprocess.Popen(command)
        while process.poll() is None:
            if not filesystem_capacity_healthy(
                [project, state],
                minimum_free_bytes=int(config["minFreeSpaceBytes"]),
                minimum_free_inodes=int(config["minFreeInodes"]),
            ):
                print(
                    "advisor domain MCP origin stopped: filesystem reserve was crossed",
                    file=sys.stderr,
                )
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return 75
            try:
                return process.wait(timeout=DISK_GUARD_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        cleanup_runtime_artifacts(config_path)
    return int(process.returncode or 0) if process is not None else 127


def run_gateway(config_path: Path) -> int:
    config = read_private_json(config_path)
    verify_config_runtime(config)
    if not access_complete(config):
        raise DomainMcpError(
            "Cloudflare Access is not configured. Add team domain, audience, and allowed email."
        )
    command = gateway_bwrap_command(config)
    os.execv(command[0], command)
    return 127


def systemctl(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        text=True,
        capture_output=True,
        check=check,
    )


def local_health(config: dict[str, Any], timeout: float = 2.0) -> bool:
    socket_path = Path(config["gatewaySocket"])
    secret_path = Path(config["upstreamSecretFile"])
    try:
        if not socket_path_ready(socket_path):
            return False
        ensure_secret(secret_path)
        secret = secret_path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", secret):
            return False
        request = (
            "GET /__advisor_mcp_health HTTP/1.1\r\n"
            "Host: localhost\r\n"
            f"X-Advisor-Health-Secret: {secret}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            connection.sendall(request)
            response = bytearray()
            while len(response) <= 8192:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        return (
            bytes(response).startswith(b"HTTP/1.1 200 ")
            and b'"service":"advisor-domain-mcp-gateway"' in response
        )
    except (OSError, TimeoutError):
        return False


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def access_edge_preflight(
    config: dict[str, Any],
    *,
    timeout: float = 5.0,
    opener: Any | None = None,
) -> dict[str, Any]:
    if not access_complete(config):
        return {
            "ready": False,
            "status": "not_configured",
            "oauthChallenge": False,
        }
    client = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    request = urllib.request.Request(
        f"https://{config['publicHostname']}/mcp",
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "advisor-domain-mcp-preflight/1",
        },
    )
    status: int | str = "unreachable"
    challenge = ""
    try:
        with client.open(request, timeout=timeout) as response:
            status = response.status
            challenge = response.headers.get("WWW-Authenticate", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        challenge = exc.headers.get("WWW-Authenticate", "")
        exc.close()
    except (OSError, urllib.error.URLError, TimeoutError):
        pass
    normalized = challenge.lower()
    oauth_challenge = "bearer" in normalized and "resource_metadata" in normalized
    return {
        "ready": status == 401 and oauth_challenge,
        "status": status,
        "oauthChallenge": oauth_challenge,
    }


def cloudflare_api_payload(
    path: str,
    token: str,
    *,
    timeout: float = 15.0,
    opener: Any | None = None,
) -> Any:
    if not path.startswith("/") or "://" in path or any(character in path for character in "\r\n"):
        raise DomainMcpError("Cloudflare API path is invalid.")
    client = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    request = urllib.request.Request(
        CLOUDFLARE_API_ROOT + path,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "advisor-domain-mcp-hardening-audit/1",
        },
    )
    try:
        with client.open(request, timeout=timeout) as response:
            raw = response.read(MAX_CLOUDFLARE_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise DomainMcpError(f"Cloudflare API audit failed with HTTP {status}.") from exc
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise DomainMcpError("Cloudflare API audit could not reach the API.") from exc
    if len(raw) > MAX_CLOUDFLARE_RESPONSE_BYTES:
        raise DomainMcpError("Cloudflare API audit response exceeded its size limit.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainMcpError("Cloudflare API audit returned invalid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise DomainMcpError("Cloudflare API audit returned an unsuccessful response.")
    return payload


def cloudflare_api_json(
    path: str,
    token: str,
    *,
    timeout: float = 15.0,
    opener: Any | None = None,
) -> Any:
    return cloudflare_api_payload(
        path,
        token,
        timeout=timeout,
        opener=opener,
    ).get("result")


def cloudflare_page_integer(result_info: dict[str, Any], name: str) -> int:
    value = result_info.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainMcpError("Cloudflare paginated inventory metadata was malformed.")
    return value


def cloudflare_api_paginated_list(
    path: str,
    token: str,
    *,
    page_size: int = 100,
    timeout: float = 15.0,
    opener: Any | None = None,
) -> list[Any]:
    if isinstance(page_size, bool) or not 1 <= page_size <= 1000:
        raise DomainMcpError("Cloudflare API page size is invalid.")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise DomainMcpError("Cloudflare API list path is invalid.")
    base_query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"page", "per_page"}
    ]
    items: list[Any] = []
    expected_total: int | None = None
    page = 1
    while page <= MAX_CLOUDFLARE_PAGES:
        query = urllib.parse.urlencode(
            [*base_query, ("page", str(page)), ("per_page", str(page_size))]
        )
        page_path = urllib.parse.urlunsplit(("", "", parsed.path, query, ""))
        payload = cloudflare_api_payload(
            page_path,
            token,
            timeout=timeout,
            opener=opener,
        )
        result = payload.get("result")
        result_info = payload.get("result_info")
        if not isinstance(result, list) or not isinstance(result_info, dict):
            raise DomainMcpError("Cloudflare paginated inventory metadata was malformed.")

        response_page = cloudflare_page_integer(result_info, "page")
        response_per_page = cloudflare_page_integer(result_info, "per_page")
        response_count = cloudflare_page_integer(result_info, "count")
        total_count = cloudflare_page_integer(result_info, "total_count")
        if (
            response_page != page
            or response_per_page < 1
            or response_per_page > page_size
            or response_count != len(result)
            or total_count > MAX_CLOUDFLARE_LIST_ITEMS
            or len(items) + len(result) > total_count
        ):
            raise DomainMcpError("Cloudflare paginated inventory was inconsistent.")
        if expected_total is None:
            expected_total = total_count
        elif expected_total != total_count:
            raise DomainMcpError("Cloudflare paginated inventory changed during the audit.")
        items.extend(result)
        if len(items) == total_count:
            return items
        if not result:
            raise DomainMcpError("Cloudflare paginated inventory ended before total_count.")
        page += 1
    raise DomainMcpError("Cloudflare paginated inventory exceeded its page limit.")


def cloudflare_api_single_page_list(
    path: str,
    token: str,
    *,
    timeout: float = 15.0,
    opener: Any | None = None,
) -> list[Any]:
    parsed = urllib.parse.urlsplit(path)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or "page" in query
        or "per_page" in query
    ):
        raise DomainMcpError("Cloudflare single-page inventory path was invalid.")
    payload = cloudflare_api_payload(
        path,
        token,
        timeout=timeout,
        opener=opener,
    )
    result = payload.get("result")
    result_info = payload.get("result_info")
    if not isinstance(result, list) or len(result) > MAX_CLOUDFLARE_LIST_ITEMS:
        raise DomainMcpError("Cloudflare single-page inventory was malformed.")
    if result_info is None:
        return result
    if not isinstance(result_info, dict):
        raise DomainMcpError("Cloudflare single-page inventory metadata was malformed.")

    def optional_integer(name: str) -> int | None:
        value = result_info.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DomainMcpError(
                "Cloudflare single-page inventory metadata was malformed."
            )
        return value

    response_page = optional_integer("page")
    response_per_page = optional_integer("per_page")
    response_count = optional_integer("count")
    total_count = optional_integer("total_count")
    if (
        response_page not in {None, 1}
        or (
            response_per_page is not None
            and response_per_page < len(result)
        )
        or response_count not in {None, len(result)}
        or total_count not in {None, len(result)}
    ):
        raise DomainMcpError("Cloudflare single-page inventory metadata was inconsistent.")
    return result


def identity_fingerprint(config: dict[str, Any]) -> str:
    """Bind remote hardening to the endpoint and selected repository identity.

    Mutable checkout state is verified independently on every start. Including
    it here would make routine edits require an unrelated Cloudflare API audit.
    """
    identity = {
        "hostname": config.get("publicHostname"),
        "issuer": config.get("accessIssuer"),
        "audience": config.get("accessAudience"),
        "emails": config.get("allowedEmails"),
        "projectDevice": config.get("projectDevice"),
        "projectInode": config.get("projectInode"),
        "runtimeIntegrity": config.get("runtimeIntegrity"),
        "gatewaySocket": config.get("gatewaySocket"),
        "originSocket": config.get("originSocket"),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cloudflare_hardening_current(config: dict[str, Any]) -> bool:
    record = config.get("cloudflareHardening")
    if not isinstance(record, dict):
        return False
    if (
        record.get("profileVersion") != CLOUDFLARE_HARDENING_PROFILE
        or record.get("identityFingerprint") != identity_fingerprint(config)
        or not isinstance(record.get("verifiedAt"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("redirectUriFingerprint") or ""))
        or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("remoteIdentityFingerprint") or ""))
        or not re.fullmatch(r"[a-f0-9]{32}", str(record.get("zoneId") or ""))
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            str(record.get("tunnelId") or ""),
        )
        or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("connectorFingerprint") or ""))
    ):
        return False
    try:
        verified_at = datetime.fromisoformat(record["verifiedAt"])
    except ValueError:
        return False
    if verified_at.tzinfo is None:
        return False
    age = datetime.now(timezone.utc) - verified_at.astimezone(timezone.utc)
    return timedelta(0) <= age <= timedelta(hours=REMOTE_HARDENING_MAX_AGE_HOURS)


def _duration_in_range(value: Any, *, name: str, minimum: int, maximum: int) -> bool:
    try:
        seconds = duration_seconds(value, name=name)
    except DomainMcpError:
        return False
    return minimum <= seconds <= maximum


def audit_cloudflare_hardening(
    config: dict[str, Any],
    *,
    account_id: str,
    token: str,
    redirect_uri: str,
    tunnel_id: str,
    zone_id: str,
    local_tunnel_identity: str | None = None,
    local_connector_identity: dict[str, Any] | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    account_id = validate_account_id(account_id)
    tunnel_id = validate_tunnel_id(tunnel_id)
    zone_id = validate_zone_id(zone_id)
    redirect_uri = validate_redirect_uri(redirect_uri)
    encoded_account = urllib.parse.quote(account_id, safe="")
    encoded_tunnel = urllib.parse.quote(tunnel_id, safe="")
    encoded_zone = urllib.parse.quote(zone_id, safe="")
    organization = cloudflare_api_json(
        f"/accounts/{encoded_account}/access/organizations",
        token,
        opener=opener,
    )
    tunnel = cloudflare_api_json(
        f"/accounts/{encoded_account}/cfd_tunnel/{encoded_tunnel}",
        token,
        opener=opener,
    )
    tunnel_configuration = cloudflare_api_json(
        f"/accounts/{encoded_account}/cfd_tunnel/{encoded_tunnel}/configurations",
        token,
        opener=opener,
    )
    tunnel_connections = cloudflare_api_single_page_list(
        f"/accounts/{encoded_account}/cfd_tunnel/{encoded_tunnel}/connections",
        token,
        opener=opener,
    )
    zone = cloudflare_api_json(
        f"/zones/{encoded_zone}",
        token,
        opener=opener,
    )
    dns_name = urllib.parse.quote(config["publicHostname"], safe="")
    dns_records = cloudflare_api_paginated_list(
        f"/zones/{encoded_zone}/dns_records?type=CNAME&name={dns_name}",
        token,
        opener=opener,
    )
    apps = cloudflare_api_paginated_list(
        f"/accounts/{encoded_account}/access/apps",
        token,
        opener=opener,
    )
    if not isinstance(apps, list):
        raise DomainMcpError("Cloudflare application inventory was malformed.")
    matches = [
        app
        for app in apps
        if isinstance(app, dict)
        and app.get("type") == "mcp"
        and (
            app.get("domain") == config["publicHostname"]
            or app.get("destinations")
            == [{"type": "public", "uri": config["publicHostname"]}]
        )
    ]
    application_unique = len(matches) == 1
    app = matches[0] if application_unique else {}
    app_id = str(app.get("id") or "")
    if application_unique and not re.fullmatch(r"[A-Za-z0-9-]{16,64}", app_id):
        raise DomainMcpError("Cloudflare application identifier was malformed.")

    policies: Any = []
    identity_providers: Any = []
    if application_unique:
        encoded_app = urllib.parse.quote(app_id, safe="")
        app = cloudflare_api_json(
            f"/accounts/{encoded_account}/access/apps/{encoded_app}",
            token,
            opener=opener,
        )
        policies = cloudflare_api_paginated_list(
            f"/accounts/{encoded_account}/access/apps/{encoded_app}/policies",
            token,
            opener=opener,
        )
        identity_providers = cloudflare_api_paginated_list(
            f"/accounts/{encoded_account}/access/identity_providers",
            token,
            opener=opener,
        )
    if not isinstance(app, dict):
        app = {}
    if not isinstance(policies, list):
        policies = []
    if not isinstance(identity_providers, list):
        identity_providers = []
    if not isinstance(organization, dict):
        organization = {}
    if not isinstance(tunnel, dict):
        tunnel = {}
    if not isinstance(tunnel_configuration, dict):
        tunnel_configuration = {}
    if not isinstance(tunnel_connections, list):
        tunnel_connections = []
    if not isinstance(zone, dict):
        zone = {}
    if not isinstance(dns_records, list):
        dns_records = []

    oauth = app.get("oauth_configuration")
    oauth = oauth if isinstance(oauth, dict) else {}
    registration = oauth.get("dynamic_client_registration")
    registration = registration if isinstance(registration, dict) else {}
    grant = oauth.get("grant")
    grant = grant if isinstance(grant, dict) else {}

    cloudflare_idps = [
        provider
        for provider in identity_providers
        if isinstance(provider, dict)
        and provider.get("type") == "cloudflare"
        and isinstance(provider.get("id"), str)
    ]
    restricted_idps = [
        provider
        for provider in cloudflare_idps
        if isinstance(provider.get("config"), dict)
        and provider["config"].get("restrict_to_account_members") is True
    ]
    allowed_idps = app.get("allowed_idps")
    allowed_idps = allowed_idps if isinstance(allowed_idps, list) else []
    restricted_idp_ids = {provider["id"] for provider in restricted_idps}
    expected_issuer = f"https://{str(organization.get('auth_domain') or '').lower()}"

    tunnel_config = tunnel_configuration.get("config")
    tunnel_config = tunnel_config if isinstance(tunnel_config, dict) else {}
    ingress = tunnel_config.get("ingress")
    ingress = ingress if isinstance(ingress, list) else []
    exact_ingress_indexes = [
        index
        for index, rule in enumerate(ingress)
        if isinstance(rule, dict) and rule.get("hostname") == config["publicHostname"]
    ]
    exact_ingress = (
        ingress[exact_ingress_indexes[0]]
        if len(exact_ingress_indexes) == 1
        else {}
    )

    def rule_covers_hostname(rule: Any) -> bool:
        if not isinstance(rule, dict):
            return False
        hostname = rule.get("hostname")
        if not hostname:
            return True
        if hostname == config["publicHostname"]:
            return True
        return (
            isinstance(hostname, str)
            and hostname.startswith("*.")
            and config["publicHostname"].endswith(hostname[1:])
        )

    first_covering_index = next(
        (index for index, rule in enumerate(ingress) if rule_covers_hostname(rule)),
        None,
    )
    expected_tunnel_service = f"unix:{config['gatewaySocket']}"
    route_request = exact_ingress.get("originRequest")
    route_request = route_request if isinstance(route_request, dict) else {}
    root_request = tunnel_config.get("originRequest")
    root_request = root_request if isinstance(root_request, dict) else {}
    tunnel_access = route_request.get("access", root_request.get("access"))
    tunnel_access = tunnel_access if isinstance(tunnel_access, dict) else {}
    final_catch_all = (
        bool(ingress)
        and ingress[-1] == {"service": "http_status:404"}
        and sum(
            1
            for rule in ingress
            if isinstance(rule, dict) and not rule.get("hostname")
        )
        == 1
    )

    active_connector_ids: list[str] = []
    for client in tunnel_connections:
        if not isinstance(client, dict):
            continue
        connector_id = str(client.get("id") or "").lower()
        try:
            connector_id = str(uuid.UUID(connector_id))
        except ValueError:
            continue
        connections = client.get("conns")
        connections = connections if isinstance(connections, list) else []
        active_connections = [
            connection
            for connection in connections
            if isinstance(connection, dict)
            and connection.get("is_pending_reconnect") is False
            and connection.get("client_id") == connector_id
        ]
        if active_connections:
            active_connector_ids.append(connector_id)
    active_connector_ids = sorted(set(active_connector_ids))
    active_connector_id = active_connector_ids[0] if len(active_connector_ids) == 1 else ""

    zone_account = zone.get("account")
    zone_account = zone_account if isinstance(zone_account, dict) else {}
    zone_name = str(zone.get("name") or "").lower().rstrip(".")
    exact_dns_records = [
        record
        for record in dns_records
        if isinstance(record, dict)
        and str(record.get("type") or "").upper() == "CNAME"
        and str(record.get("name") or "").lower().rstrip(".") == config["publicHostname"]
        and str(record.get("content") or "").lower().rstrip(".")
        == f"{tunnel_id}.cfargotunnel.com"
        and record.get("proxied") is True
    ]
    installed_tunnel_identity = (
        local_tunnel_id()
        if local_tunnel_identity is None
        else str(local_tunnel_identity).strip().lower()
    )
    if local_connector_identity is None:
        posture = cloudflared_posture(tunnel_id)
        local_connector_identity = {
            "tunnelId": posture.get("_localTunnelId"),
            "connectorId": posture.get("_localConnectorId"),
            "active": posture.get("localConnectorActive"),
        }
    if not isinstance(local_connector_identity, dict):
        raise DomainMcpError("Local cloudflared connector identity was malformed.")
    local_connector_tunnel = str(
        local_connector_identity.get("tunnelId") or ""
    ).strip().lower()
    local_connector_id = str(
        local_connector_identity.get("connectorId") or ""
    ).strip().lower()
    local_connector_active = local_connector_identity.get("active") is True

    expected_email = config["allowedEmails"][0] if len(config["allowedEmails"]) == 1 else ""
    exact_policy = False
    account_member_required = False
    mfa_required = False
    if len(policies) == 1 and isinstance(policies[0], dict):
        policy = policies[0]
        include = policy.get("include")
        exclude = policy.get("exclude")
        require = policy.get("require")
        include = include if isinstance(include, list) else []
        exclude = exclude if isinstance(exclude, list) else []
        require = require if isinstance(require, list) else []
        exact_policy = (
            policy.get("decision") == "allow"
            and include == [{"email": {"email": expected_email}}]
            and exclude == []
        )
        account_member_required = any(
            isinstance(rule, dict)
            and rule.get("cloudflare_account_member") == {"account_id": account_id}
            for rule in require
        )
        mfa = policy.get("mfa_config")
        mfa = mfa if isinstance(mfa, dict) else {}
        authenticators = mfa.get("allowed_authenticators")
        authenticators = authenticators if isinstance(authenticators, list) else []
        phishing_resistant_authenticators = {"security_key", "biometrics"}
        mfa_required = (
            mfa.get("mfa_disabled") is False
            and bool(authenticators)
            and all(
                isinstance(authenticator, str)
                and authenticator in phishing_resistant_authenticators
                for authenticator in authenticators
            )
            and _duration_in_range(
                mfa.get("session_duration"),
                name="MFA session duration",
                minimum=5 * 60,
                maximum=24 * 60 * 60,
            )
        )

    checks = {
        "applicationUnique": application_unique,
        "issuerBelongsToAccount": (
            expected_issuer == config["accessIssuer"]
            and expected_issuer.endswith(".cloudflareaccess.com")
        ),
        "applicationIdentityExact": (
            app.get("aud") == config["accessAudience"]
            and app.get("destinations")
            == [{"type": "public", "uri": config["publicHostname"]}]
        ),
        "managedOauthEnabled": oauth.get("enabled") is True,
        "dynamicRegistrationRestricted": (
            registration.get("enabled") is True
            and registration.get("allow_any_on_localhost") is False
            and registration.get("allow_any_on_loopback") is False
            and registration.get("allowed_uris") == [redirect_uri]
        ),
        "shortAccessToken": _duration_in_range(
            grant.get("access_token_lifetime"),
            name="OAuth access token lifetime",
            minimum=5 * 60,
            maximum=15 * 60,
        ),
        "shortGrantSession": _duration_in_range(
            grant.get("session_duration"),
            name="OAuth grant session duration",
            minimum=15 * 60,
            maximum=24 * 60 * 60,
        ),
        "shortApplicationSession": _duration_in_range(
            app.get("session_duration"),
            name="Access application session duration",
            minimum=5 * 60,
            maximum=15 * 60,
        ),
        "cloudflareIdpOnly": (
            len(allowed_idps) == 1
            and len(restricted_idp_ids) >= 1
            and allowed_idps[0] in restricted_idp_ids
            and app.get("auto_redirect_to_identity") is True
        ),
        "exactEmailPolicyOnly": exact_policy,
        "accountMembershipRequired": account_member_required,
        "phishingResistantMfaRequired": mfa_required,
        "tunnelIdentityExact": (
            tunnel.get("id") == tunnel_id
            and tunnel.get("account_tag") == account_id
            and tunnel.get("config_src") == "cloudflare"
            and tunnel_configuration.get("tunnel_id") == tunnel_id
            and (
                "account_id" not in tunnel_configuration
                or tunnel_configuration.get("account_id") == account_id
            )
            and tunnel_configuration.get("source") == "cloudflare"
        ),
        "privateUnixOriginExact": (
            len(exact_ingress_indexes) == 1
            and first_covering_index == exact_ingress_indexes[0]
            and exact_ingress.get("path") in {None, ""}
            and exact_ingress.get("service") == expected_tunnel_service
        ),
        "tunnelAccessValidationRequired": (
            tunnel_access.get("required") is True
            and tunnel_access.get("audTag") == [config["accessAudience"]]
        ),
        "finalCatchAllDeny": final_catch_all,
        "zoneIdentityExact": (
            zone.get("id") == zone_id
            and zone.get("status") == "active"
            and zone_account.get("id") == account_id
            and bool(zone_name)
            and (
                config["publicHostname"] == zone_name
                or config["publicHostname"].endswith(f".{zone_name}")
            )
        ),
        "proxiedDnsRouteExact": len(exact_dns_records) == 1 and len(dns_records) == 1,
        "singleActiveConnector": len(active_connector_ids) == 1,
        "localTunnelIdentityExact": installed_tunnel_identity == tunnel_id,
        "localConnectorIdentityExact": (
            local_connector_active
            and local_connector_tunnel == tunnel_id
            and bool(active_connector_id)
            and local_connector_id == active_connector_id
        ),
    }
    checks["ready"] = all(checks.values())
    remote_identity = {
        "accountId": account_id,
        "applicationId": app_id,
        "identityProviderIds": sorted(allowed_idps),
        "tunnelId": tunnel_id,
        "zoneId": zone_id,
        "dnsRecordIds": sorted(
            str(record.get("id") or "") for record in exact_dns_records
        ),
        "activeConnectorId": active_connector_id,
        "redirectUri": redirect_uri,
    }
    checks["_remoteIdentityFingerprint"] = hashlib.sha256(
        json.dumps(remote_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    checks["_redirectUriFingerprint"] = hashlib.sha256(
        redirect_uri.encode("utf-8")
    ).hexdigest()
    checks["_connectorFingerprint"] = hashlib.sha256(
        active_connector_id.encode("ascii")
    ).hexdigest()
    checks["_tunnelId"] = tunnel_id
    checks["_zoneId"] = zone_id
    return checks


def service_active(unit: str) -> bool:
    return systemctl("is-active", "--quiet", unit).returncode == 0


def start_services(config_path: Path) -> None:
    config = read_private_json(config_path)
    verify_config_runtime(config)
    verify_checkout_state(config)
    verify_sensitive_mask_plan(config)
    if not access_complete(config):
        raise DomainMcpError(
            "Refusing to start: Cloudflare Access identity settings are incomplete."
        )
    if not cloudflare_hardening_current(config):
        raise DomainMcpError(
            "Refusing to start: Cloudflare IdP, account-membership, MFA, OAuth redirect, "
            "private tunnel origin, and short-session hardening has not passed a recent "
            "authenticated audit."
        )
    require_system_runtime_base(Path(config["runtimeDir"]))
    if not cloudflared_socket_namespace_compatible(config):
        raise DomainMcpError(
            "Refusing to start: the tunnel socket is not below the dedicated "
            "system-visible runtime. Stop the connector and rerun prepare."
        )
    expected_tunnel_id = str(config["cloudflareHardening"]["tunnelId"])
    posture = cloudflared_posture(expected_tunnel_id)
    if not posture["active"]:
        raise DomainMcpError("Refusing to start: the named cloudflared tunnel is not active.")
    if posture["inlineTokenDetected"] or posture["environmentTokenDetected"]:
        raise DomainMcpError(
            "Refusing to start: the Cloudflare tunnel token is embedded in service metadata. "
            "Rotate it and use a root-only --token-file."
        )
    if not posture["tokenFileConfigured"] or not posture["tokenFileSafe"]:
        raise DomainMcpError(
            "Refusing to start: cloudflared must use a regular root-owned mode-0600 token file."
        )
    if not posture["tunnelIdentityExact"]:
        raise DomainMcpError(
            "Refusing to start: the root-owned cloudflared tunnel identity marker does not "
            "match the audited named tunnel."
        )
    if not posture["metricsLoopbackConfigured"]:
        raise DomainMcpError(
            "Refusing to start: cloudflared must expose diagnostics on one explicit "
            "127.0.0.1 metrics port."
        )
    if (
        not posture["localConnectorIdentityAvailable"]
        or not posture["localConnectorActive"]
        or not posture["localConnectorTunnelExact"]
    ):
        raise DomainMcpError(
            "Refusing to start: the active local cloudflared connector identity could not "
            "be matched to the audited tunnel."
        )
    connector_fingerprint = hashlib.sha256(
        str(posture["_localConnectorId"]).encode("ascii")
    ).hexdigest()
    if connector_fingerprint != config["cloudflareHardening"]["connectorFingerprint"]:
        raise DomainMcpError(
            "Refusing to start: the active local cloudflared connector is not the "
            "connector recorded by the authenticated audit."
        )
    edge = access_edge_preflight(config)
    if not edge["ready"]:
        raise DomainMcpError(
            "Refusing to start: the public MCP route did not present the expected "
            "Cloudflare Access Managed OAuth challenge."
        )
    patch_devspace(Path(config["devspaceExecutable"]), check=True)
    try:
        systemctl(
            "stop",
            GATEWAY_UNIT,
            ORIGIN_UNIT,
            EXPIRY_TIMER_UNIT,
            check=False,
        )
        systemctl("start", GATEWAY_UNIT, check=True)
        deadline = time.monotonic() + STARTUP_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if (
                local_health(config)
                and socket_path_ready(Path(config["originSocket"]))
                and service_active(GATEWAY_UNIT)
                and service_active(ORIGIN_UNIT)
                and service_active(EXPIRY_TIMER_UNIT)
            ):
                return
            time.sleep(0.25)
    except BaseException:
        stop_services(config_path)
        raise
    stop_services(config_path)
    raise DomainMcpError(
        "Gateway and DevSpace origin did not become ready; inspect the user service logs."
    )


def cleanup_runtime_artifacts(config_path: Path) -> None:
    try:
        config = read_private_json(config_path)
        runtime = Path(str(config.get("runtimeDir") or ""))
        metadata = runtime.lstat()
        if (
            not runtime.is_absolute()
            or runtime.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            return
    except (DomainMcpError, OSError):
        return
    cleanup_runtime_directory(runtime)


def cleanup_runtime_directory(runtime: Path) -> None:
    for path, expected_type in (
        (runtime / ORIGIN_RUNTIME_NAME / "devspace.sock", "socket"),
        (runtime / GATEWAY_RUNTIME_NAME / PUBLIC_GATEWAY_SOCKET_NAME, "socket"),
        (runtime / ORIGIN_RUNTIME_NAME / RUNTIME_SECRET_NAME, "file"),
        # Remove known pre-1.3 artifacts without traversing arbitrary entries.
        (runtime / "devspace.sock", "socket"),
        (runtime / PUBLIC_GATEWAY_SOCKET_NAME, "socket"),
        (runtime / RUNTIME_SECRET_NAME, "file"),
    ):
        try:
            metadata = path.lstat()
            owned = metadata.st_uid == os.getuid()
            matches = (
                stat.S_ISSOCK(metadata.st_mode)
                if expected_type == "socket"
                else stat.S_ISREG(metadata.st_mode) and not path.is_symlink()
            )
            if owned and matches:
                path.unlink()
        except OSError:
            pass


def expire_services(config_path: Path | None = None) -> None:
    systemctl("stop", GATEWAY_UNIT, ORIGIN_UNIT, EXPIRY_TIMER_UNIT, check=False)
    if config_path is not None:
        cleanup_runtime_artifacts(config_path)
    else:
        cleanup_runtime_directory(DEFAULT_RUNTIME_DIR)
    verify_services_stopped(config_path, include_timer=True)


def stop_services(config_path: Path | None = None) -> None:
    systemctl(
        "stop",
        GATEWAY_UNIT,
        ORIGIN_UNIT,
        EXPIRY_TIMER_UNIT,
        check=False,
    )
    systemctl("disable", EXPIRY_TIMER_UNIT, check=False)
    systemctl("stop", EXPIRY_SERVICE_UNIT, check=False)
    if config_path is not None:
        cleanup_runtime_artifacts(config_path)
    else:
        cleanup_runtime_directory(DEFAULT_RUNTIME_DIR)
    verify_services_stopped(config_path, include_timer=True)


def verify_services_stopped(
    config_path: Path | None,
    *,
    include_timer: bool,
    timeout: float = 5.0,
) -> None:
    units = [GATEWAY_UNIT, ORIGIN_UNIT]
    if include_timer:
        units.append(EXPIRY_TIMER_UNIT)
    deadline = time.monotonic() + timeout
    while True:
        active = [unit for unit in units if service_active(unit)]
        runtime = DEFAULT_RUNTIME_DIR
        if config_path is not None:
            try:
                config = read_private_json(config_path)
                runtime = Path(str(config.get("runtimeDir") or ""))
            except DomainMcpError:
                pass
        sockets = [
            runtime / ORIGIN_RUNTIME_NAME / "devspace.sock",
            runtime / GATEWAY_RUNTIME_NAME / PUBLIC_GATEWAY_SOCKET_NAME,
            runtime / "devspace.sock",
            runtime / PUBLIC_GATEWAY_SOCKET_NAME,
        ]
        existing_paths: list[Path] = []
        for path in sockets:
            try:
                path.lstat()
                existing_paths.append(path)
            except OSError:
                pass
        if not active and not existing_paths:
            return
        if time.monotonic() >= deadline:
            raise DomainMcpError(
                "Domain MCP shutdown did not remove every active service and private socket."
            )
        if config_path is not None:
            cleanup_runtime_artifacts(config_path)
        time.sleep(0.1)


def service_status(config_path: Path) -> dict[str, Any]:
    config = read_private_json(config_path)
    origin_active = service_active(ORIGIN_UNIT)
    gateway_active = service_active(GATEWAY_UNIT)
    timer_active = service_active(EXPIRY_TIMER_UNIT)
    return {
        "configured": access_complete(config),
        "cloudflareHardeningCurrent": cloudflare_hardening_current(config),
        "cloudflaredSocketNamespaceCompatible": (
            cloudflared_socket_namespace_compatible(config)
        ),
        "hostname": config.get("publicHostname"),
        "projectDir": config.get("projectDir"),
        "originActive": origin_active,
        "gatewayActive": gateway_active,
        "expiryTimerActive": timer_active,
        "sessionDurationMinutes": config.get("sessionDurationMinutes"),
        "resourceLimits": (
            (
                "origin_compute=full-host,"
                if config["fullCompute"]
                else (
                    f"origin_memory={int(config['originMemoryMaxBytes']) // (1024 * 1024)}MiB,"
                    f"origin_cpu={config['originCpuQuotaPercent']}%,"
                    f"file={int(config['maxFileSizeBytes']) // (1024 * 1024)}MiB,"
                )
            )
            + f"gateway_memory={int(config['gatewayMemoryMaxBytes']) // (1024 * 1024)}MiB,"
            + f"gateway_cpu={config['gatewayCpuQuotaPercent']}%,"
            + f"free_reserve={int(config['minFreeSpaceBytes']) // (1024 * 1024)}MiB"
        ),
        "gatewayHealthy": local_health(config),
        "socketReady": origin_active and socket_path_ready(Path(config["originSocket"])),
        "failClosedWindowHealthy": (
            not origin_active and not gateway_active and not timer_active
            or origin_active and gateway_active and timer_active
        ),
        "networkPolicy": config.get("networkPolicy"),
        "toolMode": config.get("toolMode"),
        "fullCompute": config.get("fullCompute"),
        "gpuMode": config.get("gpuMode"),
        "nvidiaDeviceCount": len(config.get("nvidiaDevices") or []),
        "shellIsolation": "nested-bubblewrap-per-command",
        "gatewayIsolation": "separate-bubblewrap",
        "gitMetadataReadOnly": True,
        "dirtyCheckoutApproved": config.get("dirtyCheckoutApproved"),
        "sensitivePathsMasked": len(config.get("sensitivePathMasks") or []),
        "diskProtection": (
            (
                "250ms-free-space-reserve; "
                if config["fullCompute"]
                else "per-file-limit-and-250ms-free-space-reserve; "
            )
            + "no-hard-aggregate-quota-on-original-checkout"
        ),
    }


def socket_path_ready(path: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )
    except OSError:
        return False


def parse_cloudflared_service_metadata(
    exec_start: str,
    environment: str,
) -> dict[str, Any]:
    inline_token = bool(re.search(r"(?:^|\s)--token(?:=|\s+)", exec_start))
    environment_token = bool(re.search(r"(?:^|\s)TUNNEL_TOKEN=", environment))
    match = re.search(
        r"""(?:^|\s)--token-file(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s;}\]]+))""",
        exec_start,
    )
    token_file = next((value for value in match.groups() if value), "") if match else ""
    metrics_matches = re.findall(
        r"""(?:^|\s)--metrics(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s;}\]]+))""",
        exec_start,
    )
    metrics_values = [
        next((value for value in match_groups if value), "")
        for match_groups in metrics_matches
    ]
    metrics_address = metrics_values[0] if len(metrics_values) == 1 else ""
    metrics_loopback = bool(
        re.fullmatch(
            r"127\.0\.0\.1:(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
            r"65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])",
            metrics_address,
        )
    )
    return {
        "inlineTokenDetected": inline_token,
        "environmentTokenDetected": environment_token,
        "tokenFile": token_file,
        "metricsArgumentCount": len(metrics_values),
        "metricsAddress": metrics_address if metrics_loopback else "",
        "metricsLoopbackConfigured": len(metrics_values) == 1 and metrics_loopback,
    }


def root_token_file_safe(value: str) -> bool:
    if not value:
        return False
    path = Path(value)
    if not path.is_absolute():
        return False
    try:
        path.relative_to("/etc/cloudflared")
    except ValueError:
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    file_safe = (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == 0
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )
    if not file_safe:
        return False
    for parent in path.parents:
        try:
            parent_metadata = parent.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent.is_symlink()
            or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & 0o022
        ):
            return False
    return True


def local_tunnel_id(path: Path = LOCAL_TUNNEL_ID_PATH) -> str:
    try:
        metadata = path.lstat()
    except OSError:
        return ""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) not in {0o444, 0o644}
    ):
        return ""
    for parent in path.parents:
        try:
            parent_metadata = parent.lstat()
        except OSError:
            return ""
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent.is_symlink()
            or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & 0o022
        ):
            return ""
    try:
        value = path.read_text(encoding="ascii").strip()
        return validate_tunnel_id(value)
    except (OSError, UnicodeError, DomainMcpError):
        return ""


def local_cloudflared_identity(
    metrics_address: str,
    *,
    timeout: float = 2.0,
    opener: Any | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"127\.0\.0\.1:[1-9][0-9]{0,4}", metrics_address):
        raise DomainMcpError("cloudflared diagnostics must use an explicit loopback metrics port.")
    port = int(metrics_address.rsplit(":", 1)[1])
    if port > 65535:
        raise DomainMcpError("cloudflared diagnostics metrics port is invalid.")
    client = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    request = urllib.request.Request(
        f"http://{metrics_address}/diag/tunnel",
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "advisor-domain-mcp-local-cloudflared/1",
        },
    )
    try:
        with client.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise DomainMcpError("cloudflared diagnostics did not return HTTP 200.")
            raw = response.read(MAX_CLOUDFLARED_DIAGNOSTIC_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise DomainMcpError("cloudflared diagnostics rejected the local request.") from exc
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise DomainMcpError("cloudflared diagnostics are unavailable on loopback.") from exc
    if len(raw) > MAX_CLOUDFLARED_DIAGNOSTIC_BYTES:
        raise DomainMcpError("cloudflared diagnostics exceeded the response size limit.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainMcpError("cloudflared diagnostics returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise DomainMcpError("cloudflared diagnostics returned a malformed object.")
    try:
        tunnel_id = str(uuid.UUID(str(payload.get("tunnelID") or "").lower()))
        connector_id = str(uuid.UUID(str(payload.get("connectorID") or "").lower()))
    except ValueError as exc:
        raise DomainMcpError("cloudflared diagnostics returned malformed identities.") from exc
    connections = payload.get("connections")
    if not isinstance(connections, list):
        raise DomainMcpError("cloudflared diagnostics returned malformed connections.")
    return {
        "tunnelId": tunnel_id,
        "connectorId": connector_id,
        "active": any(
            isinstance(connection, dict) and connection.get("isConnected") is True
            for connection in connections
        ),
    }


def cloudflared_posture(
    expected_tunnel_id: str = "",
    *,
    diagnostics_opener: Any | None = None,
) -> dict[str, Any]:
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "cloudflared"],
        check=False,
    ).returncode == 0
    service_path = Path("/etc/systemd/system/cloudflared.service")
    world_readable = False
    try:
        metadata = service_path.stat()
        world_readable = bool(metadata.st_mode & 0o044)
    except OSError:
        pass
    exec_start = subprocess.run(
        ["systemctl", "show", "--property=ExecStart", "--value", "cloudflared"],
        text=True,
        capture_output=True,
        check=False,
    )
    environment = subprocess.run(
        ["systemctl", "show", "--property=Environment", "--value", "cloudflared"],
        text=True,
        capture_output=True,
        check=False,
    )
    parsed = parse_cloudflared_service_metadata(
        exec_start.stdout if exec_start.returncode == 0 else "",
        environment.stdout if environment.returncode == 0 else "",
    )
    installed_tunnel_id = local_tunnel_id()
    local_identity = {"tunnelId": "", "connectorId": "", "active": False}
    local_identity_available = False
    if active and parsed["metricsLoopbackConfigured"]:
        try:
            local_identity = local_cloudflared_identity(
                str(parsed["metricsAddress"]),
                opener=diagnostics_opener,
            )
            local_identity_available = True
        except DomainMcpError:
            pass
    return {
        "active": active,
        "servicePath": str(service_path),
        "inlineTokenDetected": parsed["inlineTokenDetected"],
        "environmentTokenDetected": parsed["environmentTokenDetected"],
        "tokenFileConfigured": bool(parsed["tokenFile"]),
        "tokenFileSafe": root_token_file_safe(str(parsed["tokenFile"])),
        "serviceWorldReadable": world_readable,
        "tunnelIdentityMarkerConfigured": bool(installed_tunnel_id),
        "tunnelIdentityExact": bool(expected_tunnel_id)
        and installed_tunnel_id == expected_tunnel_id,
        "metricsLoopbackConfigured": parsed["metricsLoopbackConfigured"],
        "localConnectorIdentityAvailable": local_identity_available,
        "localConnectorActive": local_identity.get("active") is True,
        "localConnectorTunnelExact": bool(expected_tunnel_id)
        and local_identity.get("tunnelId") == expected_tunnel_id,
        "_localTunnelId": local_identity.get("tunnelId") or "",
        "_localConnectorId": local_identity.get("connectorId") or "",
    }


def print_status(payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        if isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = str(value)
        print(f"{key}: {rendered}")


def command_prepare(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    with lifecycle_lock(config_path):
        stop_services(config_path if config_path.exists() else None)
        config = prepare_config(args)
        stop_services(config_path)
    print("Advisor domain MCP prepared.")
    print(f"config: {config_path}")
    print(f"project: {config['projectDir']}")
    print(f"connector_url: https://{config['publicHostname']}/mcp")
    print(f"cloudflare_origin_service: unix:{config['gatewaySocket']}")
    print("workspace_path_for_chatgpt: /workspace")
    print("tool_mode: full")
    print("network_policy: isolated")
    print(f"dirty_checkout_approved: {'yes' if config['dirtyCheckoutApproved'] else 'no'}")
    print(f"sensitive_paths_masked: {len(config['sensitivePathMasks'])}")
    print(f"max_concurrent_operations: {config['maxConcurrent']}")
    print(f"automatic_shutdown_minutes: {config['sessionDurationMinutes']}")
    print(f"gpu_mode: {config['gpuMode']}")
    print(
        "resource_limits: "
        + (
            "origin_compute=full-host, "
            if config["fullCompute"]
            else (
                f"origin_memory={config['originMemoryMaxBytes'] // (1024 * 1024)}MiB, "
                f"origin_cpu={config['originCpuQuotaPercent']}%, "
                f"file={config['maxFileSizeBytes'] // (1024 * 1024)}MiB, "
            )
        )
        + f"gateway_memory={config['gatewayMemoryMaxBytes'] // (1024 * 1024)}MiB, "
        + f"gateway_cpu={config['gatewayCpuQuotaPercent']}%, "
        + f"free_reserve={config['minFreeSpaceBytes'] // (1024 * 1024)}MiB"
    )
    print(f"cloudflare_access_configured: {'yes' if access_complete(config) else 'no'}")
    print(
        "cloudflare_hardening_current: "
        f"{'yes' if cloudflare_hardening_current(config) else 'no'}"
    )
    if not access_complete(config):
        print("next: configure Cloudflare Access before starting the local origin")
    return 0


def command_configure_access(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    with lifecycle_lock(config_path):
        config = read_private_json(config_path)
        verify_config_runtime(config)
        stop_services(config_path)
        config["accessIssuer"] = f"https://{normalize_team_domain(args.team_domain)}"
        config["accessAudience"] = args.audience.strip()
        config["allowedEmails"] = [validate_email(args.email)]
        if len(config["accessAudience"]) < 8 or len(config["accessAudience"]) > 256:
            raise DomainMcpError("Cloudflare Access application audience is malformed.")
        config.pop("cloudflareHardening", None)
        atomic_json(config_path, config)
        write_gateway_runtime_config(config)
        install_units(config, config_path)
    print("Cloudflare Access identity settings saved.")
    print(f"connector_url: https://{config['publicHostname']}/mcp")
    return 0


def command_cloudflare_audit(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    with lifecycle_lock(config_path):
        stop_services(config_path)
        return command_cloudflare_audit_locked(args, config_path)


def command_cloudflare_audit_locked(
    args: argparse.Namespace,
    config_path: Path,
) -> int:
    config = read_private_json(config_path)
    verify_config_runtime(config)
    verify_checkout_state(config)
    verify_sensitive_mask_plan(config)
    if not access_complete(config) or len(config["allowedEmails"]) != 1:
        raise DomainMcpError("Configure exactly one Cloudflare Access identity before auditing.")
    config.pop("cloudflareHardening", None)
    atomic_json(config_path, config)
    token_path = Path(os.path.abspath(Path(args.api_token_file).expanduser()))
    token = read_private_token(token_path)
    try:
        checks = audit_cloudflare_hardening(
            config,
            account_id=args.account_id,
            token=token,
            redirect_uri=args.redirect_uri,
            tunnel_id=args.tunnel_id,
            zone_id=args.zone_id,
        )
    finally:
        token = ""
    print_status(
        {
            f"cloudflareHardening_{key}": value
            for key, value in checks.items()
            if not key.startswith("_")
        }
    )
    if not checks["ready"]:
        print("Cloudflare hardening audit failed closed; no valid attestation was retained.")
        return 2
    config["cloudflareHardening"] = {
        "profileVersion": CLOUDFLARE_HARDENING_PROFILE,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "identityFingerprint": identity_fingerprint(config),
        "redirectUriFingerprint": checks["_redirectUriFingerprint"],
        "remoteIdentityFingerprint": checks["_remoteIdentityFingerprint"],
        "connectorFingerprint": checks["_connectorFingerprint"],
        "tunnelId": checks["_tunnelId"],
        "zoneId": checks["_zoneId"],
    }
    atomic_json(config_path, config)
    print(
        "Cloudflare hardening verified. The local attestation is valid for "
        f"{REMOTE_HARDENING_MAX_AGE_HOURS} hours."
    )
    return 0


def command_start(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    with lifecycle_lock(config_path):
        start_services(config_path)
    return 0


def command_stop(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    with lifecycle_lock(config_path):
        stop_services(config_path if config_path.exists() else None)
    return 0


def command_expire(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    with lifecycle_lock(config_path):
        expire_services(config_path if config_path.exists() else None)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = read_private_json(config_path)
    verify_config_runtime(config)
    verify_checkout_state(config)
    verify_sensitive_mask_plan(config)
    patch_devspace(Path(config["devspaceExecutable"]), check=True)
    hardening = config.get("cloudflareHardening")
    expected_tunnel_id = (
        str(hardening.get("tunnelId") or "") if isinstance(hardening, dict) else ""
    )
    posture = cloudflared_posture(expected_tunnel_id)
    status = service_status(config_path)
    edge = access_edge_preflight(config)
    print_status(
        {
            **status,
            **{
                f"cloudflared_{key}": value
                for key, value in posture.items()
                if not key.startswith("_")
            },
            **{f"accessEdge_{key}": value for key, value in edge.items()},
        }
    )
    unhealthy = False
    if posture["inlineTokenDetected"] or posture["environmentTokenDetected"]:
        print(
            "warning: rotate the Cloudflare tunnel token and move it to a root-only token file; "
            "the current service metadata exposes it more broadly than intended."
        )
        unhealthy = True
    elif not posture["tokenFileConfigured"] or not posture["tokenFileSafe"]:
        print("warning: cloudflared is not using a regular root-owned mode-0600 token file.")
        unhealthy = True
    if not posture["tunnelIdentityExact"]:
        print("warning: the root-owned cloudflared tunnel identity marker is missing or stale.")
        unhealthy = True
    if not posture["metricsLoopbackConfigured"]:
        print(
            "warning: cloudflared does not have one explicit loopback-only --metrics address."
        )
        unhealthy = True
    elif (
        not posture["localConnectorIdentityAvailable"]
        or not posture["localConnectorActive"]
        or not posture["localConnectorTunnelExact"]
    ):
        print(
            "warning: the active local cloudflared connector identity could not be "
            "verified from loopback diagnostics."
        )
        unhealthy = True
    elif isinstance(hardening, dict):
        local_connector_fingerprint = hashlib.sha256(
            str(posture["_localConnectorId"]).encode("ascii")
        ).hexdigest()
        if local_connector_fingerprint != hardening.get("connectorFingerprint"):
            print(
                "warning: the active local cloudflared connector differs from the "
                "authenticated audit."
            )
            unhealthy = True
    if not posture["active"]:
        print("warning: the named cloudflared tunnel is not active.")
        unhealthy = True
    if not access_complete(config):
        print("warning: Cloudflare Access settings are incomplete; services remain fail-closed.")
        return 2
    if not cloudflare_hardening_current(config):
        print(
            "warning: Cloudflare IdP/account-membership/MFA/redirect/session hardening "
            "and private tunnel-origin hardening do not have a current authenticated audit."
        )
        unhealthy = True
    if not status["cloudflaredSocketNamespaceCompatible"]:
        print(
            "warning: the gateway socket is hidden from the hardened system cloudflared "
            "service; stop the connector and rerun prepare."
        )
        unhealthy = True
    if not edge["ready"]:
        print("warning: the public route is not presenting the Managed OAuth MCP challenge.")
        unhealthy = True
    if (
        status["originActive"] or status["gatewayActive"]
    ) and not status["failClosedWindowHealthy"]:
        print("warning: MCP services are active without the required automatic shutdown timer.")
        unhealthy = True
    return 2 if unhealthy else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Prepare config, patches, and user services.")
    prepare.add_argument("--project-dir", required=True)
    prepare.add_argument("--hostname", default=DEFAULT_HOSTNAME)
    prepare.add_argument("--team-domain", default="")
    prepare.add_argument("--audience", default="")
    prepare.add_argument("--email", default="")
    prepare.add_argument(
        "--max-concurrent",
        type=int,
        default=DEFAULT_MAX_CONCURRENT,
        help=(
            "Maximum simultaneous authenticated MCP operations "
            f"(default: {DEFAULT_MAX_CONCURRENT}, maximum: {MAX_CONCURRENT_LIMIT})."
        ),
    )
    prepare.add_argument("--max-body-bytes", type=int, default=16 * 1024 * 1024)
    prepare.add_argument("--session-minutes", type=int, default=DEFAULT_SESSION_MINUTES)
    prepare.add_argument("--origin-memory-mb", type=int, default=DEFAULT_ORIGIN_MEMORY_MB)
    prepare.add_argument("--gateway-memory-mb", type=int, default=DEFAULT_GATEWAY_MEMORY_MB)
    prepare.add_argument("--origin-cpu-percent", type=int, default=DEFAULT_ORIGIN_CPU_PERCENT)
    prepare.add_argument("--gateway-cpu-percent", type=int, default=DEFAULT_GATEWAY_CPU_PERCENT)
    prepare.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_MB)
    prepare.add_argument("--min-free-space-mb", type=int, default=DEFAULT_MIN_FREE_SPACE_MB)
    prepare.add_argument("--min-free-inodes", type=int, default=DEFAULT_MIN_FREE_INODES)
    prepare.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    prepare.add_argument("--bwrap", default="bwrap")
    prepare.add_argument("--node", default="node")
    prepare.add_argument("--devspace", default="devspace")
    prepare.add_argument(
        "--enable-nvidia",
        action="store_true",
        help=(
            "Expose only the pinned NVIDIA compute device nodes to the outer "
            "and per-command Bubblewrap sandboxes."
        ),
    )
    prepare.add_argument(
        "--full-compute",
        action="store_true",
        help=(
            "Remove origin CPU, RAM, swap, task, open-file, and per-file-size "
            "ceilings while retaining sandbox and disk-reserve enforcement."
        ),
    )
    prepare.add_argument(
        "--allow-dirty-checkout",
        action="store_true",
        help="Expose the exact current dirty Git state after explicit operator review.",
    )
    prepare.set_defaults(handler=command_prepare)

    configure = subparsers.add_parser(
        "configure-access",
        help="Save Cloudflare Access issuer, application audience, and allowed email.",
    )
    configure.add_argument("--team-domain", required=True)
    configure.add_argument("--audience", required=True)
    configure.add_argument("--email", required=True)
    configure.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    configure.set_defaults(handler=command_configure_access)

    cloudflare_audit = subparsers.add_parser(
        "audit-cloudflare",
        help="Verify the remote Cloudflare IdP, policy, MFA, OAuth, and session posture.",
    )
    cloudflare_audit.add_argument("--account-id", required=True)
    cloudflare_audit.add_argument("--tunnel-id", required=True)
    cloudflare_audit.add_argument("--zone-id", required=True)
    cloudflare_audit.add_argument("--api-token-file", required=True)
    cloudflare_audit.add_argument("--redirect-uri", required=True)
    cloudflare_audit.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    cloudflare_audit.set_defaults(handler=command_cloudflare_audit)

    for name in ("start", "stop", "status", "doctor"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
        if name == "start":
            command.set_defaults(handler=command_start)
        elif name == "stop":
            command.set_defaults(handler=command_stop)
        elif name == "status":
            command.set_defaults(
                handler=lambda args: (
                    print_status(service_status(Path(args.config).expanduser().resolve())),
                    0,
                )[1]
            )
        else:
            command.set_defaults(handler=command_doctor)

    run_origin_parser = subparsers.add_parser("run-origin", help=argparse.SUPPRESS)
    run_origin_parser.add_argument("--config", required=True)
    run_origin_parser.set_defaults(
        handler=lambda args: run_origin(Path(args.config).expanduser().resolve())
    )

    run_gateway_parser = subparsers.add_parser("run-gateway", help=argparse.SUPPRESS)
    run_gateway_parser.add_argument("--config", required=True)
    run_gateway_parser.set_defaults(
        handler=lambda args: run_gateway(Path(args.config).expanduser().resolve())
    )

    expire_parser = subparsers.add_parser("expire", help=argparse.SUPPRESS)
    expire_parser.add_argument("--config", required=True)
    expire_parser.set_defaults(handler=command_expire)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (DomainMcpError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"advisor domain MCP error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
