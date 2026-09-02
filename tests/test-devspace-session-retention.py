#!/usr/bin/env python3
"""Verify that abandoned MCP transports expire instead of accumulating."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
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


def mcp_request(
    port: int,
    secret: str,
    payload: dict[str, object],
    *,
    session_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-Advisor-Gateway-Secret": secret,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    connection.request(
        "POST",
        "/mcp",
        body=json.dumps(payload),
        headers=headers,
    )
    response = connection.getresponse()
    status = response.status
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    body = response.read()
    connection.close()
    return status, response_headers, body


def jsonrpc_body(body: bytes) -> dict[str, object]:
    text = body.decode("utf-8")
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"MCP response did not contain JSON-RPC data: {text!r}")


def initialize(port: int, secret: str) -> str:
    status, headers, _body = mcp_request(
        port,
        secret,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "session-retention-test", "version": "1"},
            },
        },
    )
    session_id = headers.get("mcp-session-id")
    if status != 200 or not session_id:
        raise AssertionError(
            f"MCP initialize failed: status={status}, session={session_id!r}"
        )
    return session_id


def call_tool(
    port: int,
    secret: str,
    session_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    status, _headers, body = mcp_request(
        port,
        secret,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session_id=session_id,
    )
    if status != 200:
        raise AssertionError(f"Tool {name} failed with HTTP {status}: {body!r}")
    return jsonrpc_body(body)


def stale_session_status(port: int, secret: str, session_id: str) -> int:
    status, _headers, _body = mcp_request(
        port,
        secret,
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
        session_id=session_id,
    )
    return status


def main() -> None:
    executable = shutil.which("devspace")
    if not executable:
        raise AssertionError("devspace is not installed.")

    with tempfile.TemporaryDirectory(prefix="devspace-session-retention-") as temporary:
        root = Path(temporary)
        config_dir = root / "config"
        project = root / "project"
        config_dir.mkdir(mode=0o700)
        project.mkdir()
        port = reserve_port()
        secret = "test-gateway-secret-" + "x" * 32
        secret_file = root / "gateway-secret"
        secret_file.write_text(secret + "\n", encoding="utf-8")
        os.chmod(secret_file, 0o600)
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
                "DEVSPACE_MCP_SESSION_IDLE_TTL_MS": "1000",
                "DEVSPACE_STATE_DIR": str(root / "state"),
                "DEVSPACE_TOOL_MODE": "full",
                "DEVSPACE_TRUSTED_PROXY_AUTH_FILE": str(secret_file),
                "DEVSPACE_WORKTREE_ROOT": str(root / "worktrees"),
                "DEVSPACE_SKILLS": "false",
                "DEVSPACE_SUBAGENTS": "false",
                "DEVSPACE_WIDGETS": "off",
            }
        )
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
            session_id = initialize(port, secret)
            initialized_status = stale_session_status(port, secret, session_id)
            if initialized_status != 202:
                raise AssertionError(
                    "MCP initialized notification returned "
                    f"HTTP {initialized_status}, expected 202."
                )

            opened = call_tool(
                port,
                secret,
                session_id,
                2,
                "open_workspace",
                {"path": str(project), "mode": "checkout"},
            )
            workspace_id = opened["result"]["structuredContent"]["workspaceId"]
            long_result: list[dict[str, object]] = []
            long_error: list[BaseException] = []

            def run_long_request() -> None:
                try:
                    long_result.append(
                        call_tool(
                            port,
                            secret,
                            session_id,
                            3,
                            "bash",
                            {
                                "workspaceId": workspace_id,
                                "command": "sleep 1.6; printf active-request-ok",
                                "timeout": 5,
                            },
                        )
                    )
                except BaseException as exc:  # pragma: no cover - assertion relay
                    long_error.append(exc)

            worker = threading.Thread(target=run_long_request, daemon=True)
            worker.start()
            time.sleep(0.2)
            ping_status, _ping_headers, ping_body = mcp_request(
                port,
                secret,
                {"jsonrpc": "2.0", "id": 4, "method": "ping"},
                session_id=session_id,
            )
            if ping_status != 200:
                raise AssertionError(
                    f"Concurrent MCP ping failed with HTTP {ping_status}: {ping_body!r}"
                )
            worker.join(timeout=4)
            if worker.is_alive():
                raise AssertionError("Long MCP request did not complete.")
            if long_error:
                raise long_error[0]
            if not long_result:
                raise AssertionError("Long MCP request returned no result.")

            followup_status, _followup_headers, followup_body = mcp_request(
                port,
                secret,
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                session_id=session_id,
            )
            if followup_status != 200:
                raise AssertionError(
                    "Session expired during an active concurrent request: "
                    f"HTTP {followup_status}, body={followup_body!r}"
                )
            time.sleep(1.4)
            status = stale_session_status(port, secret, session_id)
            if status != 404:
                raise AssertionError(
                    f"Expired MCP session returned HTTP {status}, expected 404."
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    print("DevSpace MCP session retention test passed.")


if __name__ == "__main__":
    main()
