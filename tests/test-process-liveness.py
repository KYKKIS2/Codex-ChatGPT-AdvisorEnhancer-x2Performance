#!/usr/bin/env python3
"""Verify advisor liveness probes never terminate the process they inspect."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "codex-skill"
    / "external-advisor"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import activity_monitor  # noqa: E402
import advisor_concurrency as concurrency  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "devspace", "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = concurrency.process_identity(child.pid)
        require(concurrency.process_alive(child.pid, identity), "live child was reported dead")
        time.sleep(0.2)
        require(child.poll() is None, "process_alive terminated its target")

        require(
            not concurrency.process_alive(child.pid, "deliberately-wrong-identity"),
            "identity mismatch was reported alive",
        )
        require(child.poll() is None, "identity check terminated its target")

        require(
            activity_monitor.expected_devspace_process(child.pid),
            "activity monitor rejected the expected devspace command line",
        )
        time.sleep(0.2)
        require(child.poll() is None, "activity monitor terminated its target")
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)

    require(not concurrency.process_alive(child.pid), "terminated child was reported alive")
    print("PASS: process-liveness probes are non-destructive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
