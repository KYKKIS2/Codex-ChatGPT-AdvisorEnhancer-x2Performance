#!/usr/bin/env python3
"""Smoke-test ordinary full-mode DevSpace after secure-origin patching."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until_listening(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"DevSpace exited before listening:\n{output}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("DevSpace did not listen within 10 seconds.")


def main() -> None:
    executable = shutil.which("devspace")
    if not executable:
        raise AssertionError("devspace is not installed.")

    with tempfile.TemporaryDirectory(prefix="devspace-plain-full-test-") as temporary:
        root = Path(temporary)
        config_dir = root / "config"
        project = root / "project"
        config_dir.mkdir(mode=0o700)
        project.mkdir()
        port = reserve_port()
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "allowedRoots": [str(project)],
                    "publicBaseUrl": f"http://127.0.0.1:{port}",
                }
            ),
            encoding="utf-8",
        )
        (config_dir / "auth.json").write_text(
            json.dumps({"ownerToken": "test-owner-token-" + "x" * 32}),
            encoding="utf-8",
        )
        os.chmod(config_dir / "config.json", 0o600)
        os.chmod(config_dir / "auth.json", 0o600)

        environment = os.environ.copy()
        environment.update(
            {
                "DEVSPACE_CONFIG_DIR": str(config_dir),
                "DEVSPACE_TOOL_MODE": "full",
                "DEVSPACE_STATE_DIR": str(root / "state"),
                "DEVSPACE_WORKTREE_ROOT": str(root / "worktrees"),
                "DEVSPACE_SKILLS": "false",
                "DEVSPACE_SUBAGENTS": "false",
                "DEVSPACE_WIDGETS": "off",
            }
        )
        environment.pop("DEVSPACE_PROCESS_MAX_ACTIVE", None)
        process = subprocess.Popen(
            [executable, "serve"],
            cwd=project,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_until_listening(port, process)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    print("Plain full-mode DevSpace startup test passed.")


if __name__ == "__main__":
    main()
