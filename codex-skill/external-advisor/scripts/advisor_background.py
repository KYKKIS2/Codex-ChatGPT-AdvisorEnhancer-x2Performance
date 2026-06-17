#!/usr/bin/env python3
"""Launch advisor.py in an auditable background run directory.

This avoids fragile shell patterns where the monitored PID, response file, and
stderr log can disagree. The foreground command returns after starting a monitor
process; the monitor starts advisor.py, waits for it, and writes final status.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_ENV_KEYS = (
    "ADVISOR_PROVIDER",
    "ADVISOR_BASE_URL",
    "ADVISOR_MODEL",
    "ADVISOR_REASONING_EFFORT",
    "ADVISOR_THINKING_EFFORT",
    "ADVISOR_CHATGPT_THINKING_EFFORT",
    "ADVISOR_INTELLIGENCE",
    "ADVISOR_MAX_OUTPUT_TOKENS",
    "ADVISOR_TIMEOUT",
    "ADVISOR_PERSIST_CONVERSATION",
    "ADVISOR_TEMPORARY",
    "ADVISOR_AUTO_CREATE_PROJECT",
    "ADVISOR_SYNC_REMOTE",
    "ADVISOR_CONVERSATION_KEY",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def byte_count(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def tail_text(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def default_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / ".codex-advisor" / "background-runs" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def advisor_script() -> Path:
    return Path(__file__).resolve().with_name("advisor.py")


def command_from_args(remainder: list[str]) -> list[str]:
    if not remainder:
        raise SystemExit("Pass advisor.py arguments after --, for example: advisor_background.py -- --prompt 'Review this'")
    return [sys.executable, str(advisor_script()), *remainder]


def safe_env_snapshot() -> dict[str, str]:
    return {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}


def launch(args: argparse.Namespace, remainder: list[str]) -> int:
    run_dir = Path(args.run_dir).resolve() if args.run_dir else default_run_dir().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    command = command_from_args(remainder)

    meta = {
        "run_id": run_dir.name,
        "created_at_utc": utc_now(),
        "launcher_pid": os.getpid(),
        "cwd": str(Path.cwd().resolve()),
        "command": command,
        "env_fingerprint": safe_env_snapshot(),
        "monitor_file": str(run_dir / "monitor.log"),
        "response_file": str(run_dir / "response.md"),
        "stderr_file": str(run_dir / "stderr.log"),
        "status_file": str(run_dir / "status.json"),
        "heartbeat_file": str(run_dir / "heartbeat.json"),
    }
    write_json(run_dir / "meta.json", meta)
    write_json(run_dir / "status.json", {"state": "launching", "created_at_utc": meta["created_at_utc"]})

    monitor_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--monitor",
        str(run_dir),
        "--",
        *command,
    ]
    monitor_log = (run_dir / "monitor.log").open("ab")
    monitor = subprocess.Popen(
        monitor_cmd,
        cwd=Path.cwd(),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=monitor_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    meta["monitor_pid"] = monitor.pid
    write_json(run_dir / "meta.json", meta)

    print(f"Started advisor background run: {run_dir}")
    print(f"Monitor PID: {monitor.pid}")
    print(f"Status: {run_dir / 'status.json'}")
    print(f"Response: {run_dir / 'response.md'}")
    print(f"Stderr: {run_dir / 'stderr.log'}")
    return 0


def monitor(run_dir: Path, command: list[str]) -> int:
    status_path = run_dir / "status.json"
    heartbeat_path = run_dir / "heartbeat.json"
    response_tmp = run_dir / "response.md.tmp"
    response_path = run_dir / "response.md"
    stderr_path = run_dir / "stderr.log"
    started_at = time.time()
    started_iso = utc_now()
    child: subprocess.Popen[bytes] | None = None

    try:
        with response_tmp.open("wb") as stdout_file, stderr_path.open("ab") as stderr_file:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=Path.cwd(),
                env=os.environ.copy(),
                start_new_session=True,
            )
            running_status = {
                "state": "running",
                "started_at_utc": started_iso,
                "monitor_pid": os.getpid(),
                "child_pid": child.pid,
                "command": command,
            }
            write_json(status_path, running_status)

            while True:
                code = child.poll()
                write_json(
                    heartbeat_path,
                    {
                        "state": "running" if code is None else "finishing",
                        "timestamp_utc": utc_now(),
                        "monitor_pid": os.getpid(),
                        "child_pid": child.pid,
                        "elapsed_seconds": round(time.time() - started_at, 3),
                        "bytes_response_tmp": byte_count(response_tmp),
                        "bytes_stderr": byte_count(stderr_path),
                    },
                )
                if code is not None:
                    break
                time.sleep(5)

        response_tmp.replace(response_path)
        duration = round(time.time() - started_at, 3)
        stderr_tail = tail_text(stderr_path)
        response_bytes = byte_count(response_path)
        state = "succeeded" if child.returncode == 0 and response_bytes > 0 else "failed"
        error_summary = None
        if state == "failed":
            if child.returncode != 0:
                error_summary = f"advisor.py exited with code {child.returncode}"
            elif response_bytes == 0:
                error_summary = "advisor.py exited successfully but produced an empty response"
            if stderr_tail:
                error_summary = f"{error_summary}; stderr tail: {stderr_tail[-500:]}"
        write_json(
            status_path,
            {
                "state": state,
                "exit_code": child.returncode,
                "started_at_utc": started_iso,
                "finished_at_utc": utc_now(),
                "duration_seconds": duration,
                "monitor_pid": os.getpid(),
                "child_pid": child.pid,
                "bytes_response": response_bytes,
                "bytes_stderr": byte_count(stderr_path),
                "error_summary": error_summary,
            },
        )
        return 0 if state == "succeeded" else 1
    except Exception as exc:
        if response_tmp.exists() and not response_path.exists():
            response_tmp.replace(response_path)
        write_json(
            status_path,
            {
                "state": "failed",
                "started_at_utc": started_iso,
                "finished_at_utc": utc_now(),
                "duration_seconds": round(time.time() - started_at, 3),
                "monitor_pid": os.getpid(),
                "child_pid": child.pid if child else None,
                "exit_code": child.returncode if child else None,
                "bytes_response": byte_count(response_path),
                "bytes_stderr": byte_count(stderr_path),
                "error_summary": f"background monitor failed: {exc}",
            },
        )
        return 1


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", help="Run directory. Defaults to .codex-advisor/background-runs/<timestamp-id>.")
    parser.add_argument("--monitor", help=argparse.SUPPRESS)
    args, remainder = parser.parse_known_args()
    if remainder and remainder[0] == "--":
        remainder = remainder[1:]
    return args, remainder


def main() -> int:
    args, remainder = parse_args()
    if args.monitor:
        return monitor(Path(args.monitor), remainder)
    return launch(args, remainder)


if __name__ == "__main__":
    raise SystemExit(main())
