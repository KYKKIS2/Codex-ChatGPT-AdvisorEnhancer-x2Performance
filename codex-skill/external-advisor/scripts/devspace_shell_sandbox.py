#!/usr/bin/env python3
"""Execute one DevSpace shell command in a credential-free nested Bubblewrap sandbox."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn


WORKSPACE = Path("/workspace")
MAX_PAYLOAD_BYTES = 128 * 1024
NVIDIA_DEVICE_PATTERN = re.compile(
    r"/dev/nvidia(?:ctl|[0-9]+|-(?:uvm|uvm-tools|modeset))"
)


def fail(message: str) -> NoReturn:
    print(f"secure DevSpace shell refused command: {message}", file=sys.stderr)
    raise SystemExit(126)

def decode_payload(value: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,174764}", value):
        fail("invalid command envelope")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + padding)
        if len(raw) > MAX_PAYLOAD_BYTES:
            fail("command envelope is too large")
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        fail("invalid command envelope")
    if not isinstance(payload, dict):
        fail("command envelope must be an object")
    return payload


def validated_command(payload: dict[str, Any]) -> tuple[str, Path]:
    command = payload.get("command")
    cwd_value = payload.get("cwd")
    root_value = payload.get("root")
    if not isinstance(command, str) or not command or len(command.encode("utf-8")) > 64 * 1024:
        fail("command must be between 1 and 65536 bytes")
    if "\0" in command:
        fail("command contains a null byte")
    if root_value != str(WORKSPACE) or not isinstance(cwd_value, str):
        fail("workspace boundary is not pinned")
    try:
        cwd = Path(cwd_value).resolve(strict=True)
        metadata = cwd.lstat()
        cwd.relative_to(WORKSPACE)
    except (OSError, ValueError):
        fail("working directory is outside the pinned workspace")
    if not stat.S_ISDIR(metadata.st_mode) or cwd.is_symlink():
        fail("working directory must be a real directory")
    return command, cwd


def sandbox_etc_arguments() -> list[str]:
    # The outer origin already exposes a curated /etc. Rebinding that stable
    # snapshot avoids races when the host atomically replaces files such as
    # ld.so.cache while a connector window is active.
    return ["--ro-bind", "/etc", "/etc"]


def nvidia_device_paths() -> list[Path]:
    raw = os.environ.get("ADVISOR_PINNED_NVIDIA_DEVICES", "")
    if not raw:
        return []
    if len(raw) > 4096:
        fail("NVIDIA device policy is too large")
    values = raw.split(":")
    if len(values) != len(set(values)) or any(
        not NVIDIA_DEVICE_PATTERN.fullmatch(value) for value in values
    ):
        fail("NVIDIA device policy is malformed")
    paths = [Path(value) for value in values]
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError:
            fail("a pinned NVIDIA device is unavailable")
        if path.is_symlink() or not stat.S_ISCHR(metadata.st_mode):
            fail("a pinned NVIDIA device is unsafe")
    rendered = {str(path) for path in paths}
    if (
        "/dev/nvidiactl" not in rendered
        or "/dev/nvidia-uvm" not in rendered
        or not any(re.fullmatch(r"/dev/nvidia[0-9]+", value) for value in rendered)
    ):
        fail("NVIDIA compute devices are incomplete")
    return paths


def nested_bwrap_command(command: str, cwd: Path) -> list[str]:
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file():
        fail("Bubblewrap is unavailable")
    git_metadata = WORKSPACE / ".git"
    if git_metadata.is_symlink() or not git_metadata.is_dir():
        fail("the pinned workspace no longer has a real Git metadata directory")

    arguments = [
        str(bwrap),
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
    ]
    if Path("/opt/node").is_dir():
        arguments.extend(["--ro-bind", "/opt/node", "/opt/node"])
    arguments.extend(
        [
            "--dir",
            "/home",
            "--dir",
            "/home/devspace",
            "--dir",
            "/run",
            "--bind",
            str(WORKSPACE),
            str(WORKSPACE),
            "--ro-bind",
            str(git_metadata),
            str(git_metadata),
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
    )
    for path in nvidia_device_paths():
        arguments.extend(["--dev-bind", str(path), str(path)])
    arguments.extend(["--tmpfs", "/tmp"])
    arguments.extend(sandbox_etc_arguments())
    for name, value in (
        ("HOME", "/home/devspace"),
        ("USER", "devspace"),
        ("LOGNAME", "devspace"),
        ("PATH", "/opt/node/bin:/usr/bin:/bin"),
        ("LANG", "C.UTF-8"),
        ("LC_ALL", "C.UTF-8"),
        ("NO_COLOR", "1"),
        ("TERM", "dumb"),
        ("PAGER", "cat"),
        ("GIT_PAGER", "cat"),
        ("GH_PAGER", "cat"),
        ("CODEX_CI", "1"),
    ):
        arguments.extend(["--setenv", name, value])
    arguments.extend(
        [
            "--hostname",
            "advisor-shell",
            "--chdir",
            str(cwd),
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-lc",
            command,
        ]
    )
    return arguments


def main() -> int:
    if len(sys.argv) != 2:
        fail("expected exactly one command envelope")
    payload = decode_payload(sys.argv[1])
    command, cwd = validated_command(payload)
    arguments = nested_bwrap_command(command, cwd)
    os.execv(arguments[0], arguments)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
