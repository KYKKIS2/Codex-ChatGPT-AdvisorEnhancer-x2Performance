#!/usr/bin/env python3
"""Safe local activity reporting for foreground repo-aware advisor calls."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO

import advisor_concurrency as concurrency


ACTIVITY_PREFIX = "[advisor activity]"
DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 0.2
MAX_DURATION_MS = 86_400_000
TOOL_LABELS = {
    "open_workspace": "open_workspace",
    "read": "read",
    "write": "write",
    "edit": "edit",
    "grep": "grep",
    "glob": "glob",
    "ls": "ls",
    "bash": "bash",
    "exec_command": "exec_command",
    "write_stdin": "write_stdin",
    "apply_patch": "apply_patch",
    "show_changes": "show_changes",
}
SAFE_TOOL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_OUTPUT_LOCK = threading.Lock()


def env_enabled(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def runtime_root(raw: str | Path | None = None) -> Path:
    configured = raw or os.environ.get("ADVISOR_AGENT_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return codex_home / "advisor-agent" / "devspace"


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def owned_by_current_user(path: Path) -> bool:
    if not hasattr(os, "getuid"):
        return True
    try:
        return path.stat(follow_symlinks=False).st_uid == os.getuid()
    except OSError:
        return False


def expected_devspace_process(pid: int) -> bool:
    if pid <= 0:
        return False
    if not concurrency.process_alive(pid):
        return False

    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if not proc_cmdline.exists():
        return True
    try:
        command = proc_cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    return "devspace" in command and "serve" in command


def discover_log_path(
    project_dir: Path,
    *,
    root: Path | None = None,
    process_validator: Callable[[int], bool] = expected_devspace_process,
) -> Path | None:
    expected_project = project_dir.expanduser().resolve()
    raw_root = (root or runtime_root()).expanduser()
    if raw_root.is_symlink():
        return None
    expected_root = raw_root.resolve()
    if not expected_root.is_dir() or not owned_by_current_user(expected_root):
        return None

    matches: list[Path] = []
    for state_path in expected_root.glob("*/state.json"):
        if state_path.is_symlink() or not state_path.is_file() or not owned_by_current_user(state_path):
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        raw_project = state.get("project_dir")
        raw_log = state.get("log_path")
        pid = state.get("devspace_pid", state.get("pid"))
        if not isinstance(raw_project, str) or not isinstance(raw_log, str) or not isinstance(pid, int):
            continue
        try:
            state_project = Path(raw_project).expanduser().resolve()
            raw_log_path = Path(raw_log).expanduser()
            if raw_log_path.is_symlink():
                continue
            log_path = raw_log_path.resolve(strict=False)
        except OSError:
            continue
        if state_project != expected_project or not process_validator(pid):
            continue
        if not path_is_within(log_path, expected_root) or log_path.is_symlink():
            continue
        if log_path.exists() and (not log_path.is_file() or not owned_by_current_user(log_path)):
            continue
        matches.append(log_path)

    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


class ActivityMonitor:
    """Tail one validated DevSpace log without affecting advisor transport."""

    def __init__(
        self,
        log_path: Path | None,
        *,
        output: TextIO | None = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log_path = log_path
        self.output = output or sys.stderr
        self.heartbeat_seconds = max(5.0, float(heartbeat_seconds))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active = log_path is not None
        self._last_activity = self.monotonic()
        self._initial_file_exists = bool(log_path and log_path.exists())
        self._initial_opened: tuple[TextIO, tuple[int, int]] | None = None

    @classmethod
    def for_project(
        cls,
        project_dir: Path,
        *,
        enabled: bool | None = None,
        root: Path | None = None,
        output: TextIO | None = None,
        heartbeat_seconds: float | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        process_validator: Callable[[int], bool] = expected_devspace_process,
    ) -> "ActivityMonitor":
        if enabled is None:
            enabled = env_enabled("ADVISOR_LIVE_ACTIVITY", True)
        if not enabled:
            return cls(None, output=output)
        try:
            log_path = discover_log_path(project_dir, root=root, process_validator=process_validator)
            configured_heartbeat = heartbeat_seconds
            if configured_heartbeat is None:
                raw = os.environ.get("ADVISOR_LIVE_ACTIVITY_HEARTBEAT_SECONDS", str(DEFAULT_HEARTBEAT_SECONDS))
                try:
                    configured_heartbeat = float(raw)
                except ValueError:
                    configured_heartbeat = DEFAULT_HEARTBEAT_SECONDS
            return cls(
                log_path,
                output=output,
                heartbeat_seconds=configured_heartbeat,
                poll_seconds=poll_seconds,
            )
        except Exception:
            return cls(None, output=output)

    def _emit(self, message: str) -> None:
        if self.stop_event.is_set():
            return
        try:
            with _OUTPUT_LOCK:
                print(f"{ACTIVITY_PREFIX} {message}", file=self.output, flush=True)
            self._last_activity = self.monotonic()
        except Exception:
            return

    def _emit_record(self, record: Any) -> None:
        if not isinstance(record, dict):
            return
        if record.get("event") != "tool_call":
            return
        raw_tool = record.get("tool")
        if not isinstance(raw_tool, str) or not SAFE_TOOL_RE.fullmatch(raw_tool):
            return
        label = TOOL_LABELS.get(raw_tool)
        if label is None:
            return
        success = record.get("success")
        if not isinstance(success, bool):
            return
        raw_duration = record.get("durationMs")
        duration_ms: int | None = None
        if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
            duration_ms = max(0, min(int(raw_duration), MAX_DURATION_MS))
        outcome = "completed" if success else "failed"
        suffix = f" in {duration_ms} ms" if duration_ms is not None else ""
        self._emit(f"DevSpace {label} {outcome}{suffix}.")

    def _open_log(self, *, from_start: bool) -> tuple[TextIO, tuple[int, int]] | None:
        if self.log_path is None:
            return None
        try:
            before = self.log_path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                return None
            if hasattr(os, "getuid") and before.st_uid != os.getuid():
                return None
            handle = self.log_path.open("r", encoding="utf-8", errors="replace")
            opened = os.fstat(handle.fileno())
        except OSError:
            return None
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            handle.close()
            return None
        if not from_start:
            handle.seek(0, os.SEEK_END)
        return handle, (opened.st_dev, opened.st_ino)

    def _run(self) -> None:
        opened = self._initial_opened
        self._initial_opened = None
        handle: TextIO | None = opened[0] if opened else None
        identity: tuple[int, int] | None = opened[1] if opened else None
        pending = ""
        open_from_start = not self._initial_file_exists and handle is None
        try:
            while not self.stop_event.is_set():
                if handle is None:
                    opened = self._open_log(from_start=open_from_start)
                    if opened is not None:
                        handle, identity = opened
                        pending = ""
                        open_from_start = False
                if handle is not None:
                    try:
                        chunk = handle.read()
                    except OSError:
                        handle.close()
                        handle = None
                        identity = None
                        chunk = ""
                    if chunk:
                        pending += chunk
                        lines = pending.split("\n")
                        pending = lines.pop()
                        for line in lines:
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            self._emit_record(record)
                    try:
                        current = self.log_path.lstat() if self.log_path is not None else None
                    except OSError:
                        current = None
                    if current is None or stat.S_ISLNK(current.st_mode):
                        handle.close()
                        handle = None
                        identity = None
                        open_from_start = True
                    elif identity != (current.st_dev, current.st_ino) or current.st_size < handle.tell():
                        handle.close()
                        handle = None
                        identity = None
                        pending = ""
                        open_from_start = True
                now = self.monotonic()
                if now - self._last_activity >= self.heartbeat_seconds:
                    self._emit("ChatGPT agent is still working; waiting for tool activity or the final response.")
                self.stop_event.wait(self.poll_seconds)
        except Exception:
            return
        finally:
            if handle is not None:
                handle.close()

    def start(self) -> "ActivityMonitor":
        if not self.active or self.thread is not None:
            return self
        try:
            if self._initial_file_exists:
                self._initial_opened = self._open_log(from_start=False)
            self._emit("Watching project DevSpace activity while ChatGPT works.")
            self.thread = threading.Thread(target=self._run, name="advisor-activity-monitor", daemon=True)
            self.thread.start()
        except Exception:
            if self._initial_opened is not None:
                self._initial_opened[0].close()
                self._initial_opened = None
            self.active = False
            self.thread = None
        return self

    def stop(self, *, response_received: bool = False) -> None:
        if not self.active:
            return
        try:
            if response_received:
                self._emit("ChatGPT response received; validating and saving the final answer.")
            self.stop_event.set()
            if self.thread is not None:
                self.thread.join(timeout=1.0)
        except Exception:
            return

    def __enter__(self) -> "ActivityMonitor":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.stop(response_received=exc_type is None)
        return False
