#!/usr/bin/env python3
"""Security and MCP integration tests for the permanent-domain DevSpace origin."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "codex-skill" / "external-advisor" / "scripts"
SPEC = importlib.util.spec_from_file_location("advisor_domain_mcp", SCRIPTS / "advisor_domain_mcp.py")
assert SPEC and SPEC.loader
domain_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(domain_mcp)

SHELL_SPEC = importlib.util.spec_from_file_location(
    "devspace_shell_sandbox",
    SCRIPTS / "devspace_shell_sandbox.py",
)
assert SHELL_SPEC and SHELL_SPEC.loader
shell_sandbox = importlib.util.module_from_spec(SHELL_SPEC)
SHELL_SPEC.loader.exec_module(shell_sandbox)

assert domain_mcp.DEFAULT_RUNTIME_DIR == Path(
    f"/run/advisor-domain-mcp-{os.getuid()}"
)
assert domain_mcp.STARTUP_READY_TIMEOUT_SECONDS >= 60


class UnixHttpConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=10)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def mcp_payload(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8")
    if text.startswith("event:") or "\ndata:" in text:
        data_lines = [
            line.removeprefix("data:").strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            raise AssertionError(f"MCP SSE response had no data line: {text[:400]}")
        text = data_lines[-1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise AssertionError("MCP response was not an object.")
    return payload


def unix_request(
    socket_path: Path,
    body: dict[str, Any],
    *,
    secret: str | None,
    session_id: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = UnixHttpConnection(str(socket_path))
    headers = {
        "host": "localhost",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if secret:
        headers["x-advisor-gateway-secret"] = secret
    if session_id:
        headers["mcp-session-id"] = session_id
    connection.request("POST", "/mcp", body=json.dumps(body), headers=headers)
    response = connection.getresponse()
    response_body = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, response_headers, response_body


def tool_call(
    socket_path: Path,
    secret: str,
    session_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    status, _, body = unix_request(
        socket_path,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        secret=secret,
        session_id=session_id,
    )
    assert status == 200, body.decode("utf-8", errors="replace")
    return mcp_payload(body)


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists() and path.is_socket():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"Secure origin exited early ({process.returncode}).\nstdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.1)
    raise AssertionError("Secure DevSpace origin did not create its Unix socket.")


def fixture_config(base: Path) -> tuple[dict[str, Any], Path, str]:
    project = base / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Advisor Test",
            "-c",
            "user.email=advisor-test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    metadata = project.stat()

    state = base / "state"
    runtime = base / "runtime"
    origin_runtime = runtime / domain_mcp.ORIGIN_RUNTIME_NAME
    gateway_runtime = runtime / domain_mcp.GATEWAY_RUNTIME_NAME
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    origin_runtime.mkdir(mode=0o700)
    gateway_runtime.mkdir(mode=0o700)
    secret_path = base / "origin-secret"
    secret = "s" * 64
    secret_path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(secret_path, 0o600)
    pinned = base / "pinned-root"
    pinned.write_text("/workspace\n", encoding="utf-8")
    os.chmod(pinned, 0o600)
    gateway_config = base / "gateway-runtime.json"

    node = domain_mcp.require_executable("node")
    devspace = domain_mcp.require_executable("devspace")
    node_root, devspace_dist = domain_mcp.devspace_layout(devspace, node)
    devspace_runtime_files = [
        path
        for path in devspace_dist.rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    package_json = devspace_dist.parent / "package.json"
    if package_json.is_file() and not package_json.is_symlink():
        devspace_runtime_files.append(package_json)
    for runtime_file in (devspace, *devspace_runtime_files):
        domain_mcp.harden_user_owned_runtime_file(runtime_file.resolve(strict=True))
    for runtime_file in (
        SCRIPTS / "advisor_domain_mcp.py",
        SCRIPTS / "agent_mode.py",
        SCRIPTS / "advisor_concurrency.py",
        SCRIPTS / "advisor_safety.py",
        SCRIPTS / "devspace_readonly_patch.py",
        SCRIPTS / "devspace_secure_origin_patch.py",
        SCRIPTS / "cloudflare_access_gateway.mjs",
        SCRIPTS / "devspace_secure_server.mjs",
        SCRIPTS / "devspace_shell_sandbox.py",
    ):
        domain_mcp.harden_user_owned_runtime_file(runtime_file.resolve(strict=True))
    git = domain_mcp.require_executable("git")
    git_state = domain_mcp.git_checkout_state(project, git)
    config = {
        "schemaVersion": domain_mcp.CONFIG_SCHEMA,
        "projectDir": str(project),
        "projectDevice": metadata.st_dev,
        "projectInode": metadata.st_ino,
        "publicHostname": "mcp.example.com",
        "accessIssuer": "https://advisor-test.cloudflareaccess.com",
        "accessAudience": "audience_test_value",
        "allowedEmails": ["owner@example.com"],
        "gitHead": git_state["head"],
        "gitStateFingerprint": git_state["fingerprint"],
        "gitMutableFingerprint": git_state["mutableFingerprint"],
        "gitMutableEntryCount": git_state["mutableEntryCount"],
        "dirtyCheckoutApproved": False,
        "gatewaySocket": str(gateway_runtime / domain_mcp.PUBLIC_GATEWAY_SOCKET_NAME),
        "originSocket": str(origin_runtime / "devspace.sock"),
        "upstreamSecretFile": str(secret_path),
        "gatewayRuntimeConfig": str(gateway_config),
        "maxConcurrent": domain_mcp.DEFAULT_MAX_CONCURRENT,
        "maxBodyBytes": 1024 * 1024,
        "clockSkewSeconds": 60,
        "stateDir": str(state),
        "runtimeDir": str(runtime),
        "originRuntimeDir": str(origin_runtime),
        "gatewayRuntimeDir": str(gateway_runtime),
        "pinnedRootFile": str(pinned),
        "pythonExecutable": str(Path(sys.executable).resolve()),
        "gitExecutable": str(git),
        "bwrapPath": str(domain_mcp.require_executable("bwrap")),
        "nodeRoot": str(node_root),
        "devspaceDist": str(devspace_dist),
        "devspaceExecutable": str(devspace),
        "agentModeScript": str(SCRIPTS / "agent_mode.py"),
        "advisorConcurrencyScript": str(SCRIPTS / "advisor_concurrency.py"),
        "advisorSafetyScript": str(SCRIPTS / "advisor_safety.py"),
        "readonlyPatchScript": str(SCRIPTS / "devspace_readonly_patch.py"),
        "secureOriginPatchScript": str(SCRIPTS / "devspace_secure_origin_patch.py"),
        "gatewayScript": str(SCRIPTS / "cloudflare_access_gateway.mjs"),
        "secureServerScript": str(SCRIPTS / "devspace_secure_server.mjs"),
        "shellSandboxScript": str(SCRIPTS / "devspace_shell_sandbox.py"),
        "managerScript": str(SCRIPTS / "advisor_domain_mcp.py"),
        "networkPolicy": "isolated",
        "toolMode": "full",
        "fullCompute": False,
        "gpuMode": "none",
        "nvidiaDevices": [],
        "sessionDurationMinutes": 60,
        "originMemoryMaxBytes": 2048 * 1024 * 1024,
        "gatewayMemoryMaxBytes": 256 * 1024 * 1024,
        "originCpuQuotaPercent": 200,
        "gatewayCpuQuotaPercent": 50,
        "maxFileSizeBytes": 512 * 1024 * 1024,
        "minFreeSpaceBytes": 1024 * 1024 * 1024,
        "minFreeInodes": 1000,
        "sensitivePathMasks": domain_mcp.sensitive_mask_plan(project),
    }
    domain_mcp.write_gateway_runtime_config(config)
    config["runtimeIntegrity"] = domain_mcp.runtime_integrity_manifest(config)
    return config, project, secret


def test_cloudflared_runtime_namespace_contract() -> None:
    runtime = domain_mcp.DEFAULT_RUNTIME_DIR
    config = {
        "runtimeDir": str(runtime),
        "gatewayRuntimeDir": str(runtime / domain_mcp.GATEWAY_RUNTIME_NAME),
        "gatewaySocket": str(
            runtime
            / domain_mcp.GATEWAY_RUNTIME_NAME
            / domain_mcp.PUBLIC_GATEWAY_SOCKET_NAME
        ),
    }
    assert domain_mcp.cloudflared_socket_namespace_compatible(config)
    config["runtimeDir"] = f"/run/user/{os.getuid()}/advisor-domain-mcp"
    assert not domain_mcp.cloudflared_socket_namespace_compatible(config)


def test_expiry_timer_status(config: dict[str, Any]) -> None:
    wall_now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    active = domain_mcp.expiry_timer_details(
        config,
        True,
        active_enter_monotonic_us=90_000_000,
        monotonic_now=100.0,
        wall_now=wall_now,
    )
    assert active == {
        "expiryTimerExpiresAt": "2026-07-30T08:59:50+00:00",
        "expiryTimerRemainingSeconds": 3590,
        "expiryTimerRemaining": "59m 50s",
    }
    assert domain_mcp.expiry_timer_details(config, False) == {
        "expiryTimerExpiresAt": "inactive",
        "expiryTimerRemainingSeconds": 0,
        "expiryTimerRemaining": "inactive",
    }
    assert domain_mcp.format_remaining_duration(90_061) == "1d 1h 1m 1s"


def test_manager_guards(config: dict[str, Any], base: Path) -> None:
    parsed = domain_mcp.build_parser().parse_args(
        ["prepare", "--project-dir", str(base)]
    )
    assert parsed.max_concurrent == domain_mcp.DEFAULT_MAX_CONCURRENT == 8
    assert parsed.enable_nvidia is False
    assert parsed.full_compute is False
    assert domain_mcp.build_parser().parse_args(
        [
            "prepare",
            "--project-dir",
            str(base),
            "--enable-nvidia",
            "--full-compute",
        ]
    ).enable_nvidia is True
    assert domain_mcp.build_parser().parse_args(
        ["prepare", "--project-dir", str(base), "--full-compute"]
    ).full_compute is True
    assert config["maxConcurrent"] == domain_mcp.DEFAULT_MAX_CONCURRENT
    bounded_unit = domain_mcp.origin_unit(config, base / "config.json")
    assert f"MemoryMax={config['originMemoryMaxBytes']}" in bounded_unit
    assert f"CPUQuota={config['originCpuQuotaPercent']}%" in bounded_unit
    full_compute_config = dict(config)
    full_compute_config["fullCompute"] = True
    full_unit = domain_mcp.origin_unit(full_compute_config, base / "config.json")
    assert "MemoryMax=infinity" in full_unit
    assert "MemorySwapMax=infinity" in full_unit
    assert "TasksMax=infinity" in full_unit
    assert "LimitNOFILE=infinity" in full_unit
    assert "LimitFSIZE=infinity" in full_unit
    assert "CPUQuota=" not in full_unit
    for device in (
        "/dev/nvidiactl",
        "/dev/nvidia0",
        "/dev/nvidia-uvm",
        "/dev/nvidia-uvm-tools",
        "/dev/nvidia-modeset",
    ):
        assert domain_mcp.NVIDIA_DEVICE_PATTERN.fullmatch(device)
    assert domain_mcp.nvidia_device_plan(False) == []
    assert domain_mcp.verify_nvidia_device_plan(config) == []
    malformed_gpu = dict(config)
    malformed_gpu["gpuMode"] = "none"
    malformed_gpu["nvidiaDevices"] = [
        {"path": "/dev/nvidia0", "major": 195, "minor": 0}
    ]
    try:
        domain_mcp.verify_nvidia_device_plan(malformed_gpu)
    except domain_mcp.DomainMcpError as exc:
        assert "Disabled GPU mode" in str(exc)
    else:
        raise AssertionError("disabled GPU mode accepted a device binding")

    try:
        nvidia_plan = domain_mcp.nvidia_device_plan(True)
    except domain_mcp.DomainMcpError:
        nvidia_plan = []
    if nvidia_plan:
        gpu_config = dict(config)
        gpu_config["gpuMode"] = "nvidia"
        gpu_config["nvidiaDevices"] = nvidia_plan
        gpu_arguments = domain_mcp.nvidia_bwrap_arguments(gpu_config)
        for entry in nvidia_plan:
            path = entry["path"]
            assert ["--dev-bind", path, path] == gpu_arguments[
                gpu_arguments.index(path) - 1 : gpu_arguments.index(path) + 2
            ]
        gpu_command = domain_mcp.bwrap_command(
            gpu_config,
            command=["/usr/bin/true"],
        )
        variable_index = gpu_command.index("ADVISOR_PINNED_NVIDIA_DEVICES")
        assert gpu_command[variable_index - 1] == "--setenv"
        assert gpu_command[variable_index + 1] == ":".join(
            entry["path"] for entry in nvidia_plan
        )
        assert "DEVSPACE_NVIDIA_DEVICES" not in gpu_command

    mask_project = base / "mask-project"
    mask_project.mkdir()
    (mask_project / ".codex-advisor").mkdir()
    (mask_project / ".codex-advisor" / "conversation.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (mask_project / ".env").write_text("PRIVATE_VALUE=redacted\n", encoding="utf-8")
    (mask_project / ".env.example").write_text("PRIVATE_VALUE=\n", encoding="utf-8")
    (mask_project / "private_key.txt").write_text(
        "api_key=sk-" + "a" * 32 + "\n", encoding="utf-8"
    )
    (mask_project / "notes.txt").write_text(
        "api_key=sk-" + "b" * 32 + "\n", encoding="utf-8"
    )
    (mask_project / "node_modules" / "package").mkdir(parents=True)
    (mask_project / "node_modules" / "package" / ".env").write_text(
        "PACKAGE_SECRET=redacted\n",
        encoding="utf-8",
    )
    (mask_project / "docs").mkdir()
    (mask_project / "docs" / "research.pdf").write_bytes(b"research")
    (mask_project / "artifacts").mkdir()
    (mask_project / "artifacts" / ".env").write_text(
        "INTENTIONALLY_EXPOSED_BULK_PATH=1\n",
        encoding="utf-8",
    )
    scanner = domain_mcp.load_agent_mode_module()
    resolving_marker = scanner.contains_sensitive_project_marker
    scanner.contains_sensitive_project_marker = lambda *_args: (_ for _ in ()).throw(
        AssertionError("sensitive mask scan resolved a path that was already relative")
    )
    try:
        mask_plan = domain_mcp.sensitive_mask_plan(mask_project)
    finally:
        scanner.contains_sensitive_project_marker = resolving_marker
    assert ".codex-advisor" in mask_plan
    assert ".env" in mask_plan
    assert "private_key.txt" in mask_plan
    assert "notes.txt" not in mask_plan
    assert "node_modules/package/.env" not in mask_plan
    assert "docs/research.pdf" not in mask_plan
    assert "artifacts/.env" not in mask_plan
    assert ".env.example" not in mask_plan
    assert ".git/config" in config["sensitivePathMasks"]

    missing_runtime_config = base / "missing-runtime-config.json"
    domain_mcp.atomic_json(
        missing_runtime_config,
        {"runtimeDir": str(base / "missing-runtime")},
    )
    domain_mcp.cleanup_runtime_artifacts(missing_runtime_config)

    quoted = domain_mcp.systemd_quote("/tmp/percent%value")
    assert quoted == '"/tmp/percent%%value"'
    try:
        domain_mcp.systemd_quote("unsafe\nvalue")
    except domain_mcp.DomainMcpError:
        pass
    else:
        raise AssertionError("systemd_quote accepted a newline")

    inline = domain_mcp.parse_cloudflared_service_metadata(
        "/usr/local/bin/cloudflared tunnel run --token secret-redacted",
        "",
    )
    assert inline["inlineTokenDetected"] is True
    assert inline["tokenFile"] == ""
    token_file = domain_mcp.parse_cloudflared_service_metadata(
        (
            "/usr/local/bin/cloudflared tunnel run "
            "--token-file=/etc/cloudflared/tunnel-token "
            "--metrics=127.0.0.1:60123"
        ),
        "",
    )
    assert token_file["inlineTokenDetected"] is False
    assert token_file["tokenFile"] == "/etc/cloudflared/tunnel-token"
    assert token_file["metricsAddress"] == "127.0.0.1:60123"
    assert token_file["metricsLoopbackConfigured"] is True
    unsafe_metrics = domain_mcp.parse_cloudflared_service_metadata(
        "/usr/local/bin/cloudflared tunnel run --metrics=0.0.0.0:60123",
        "",
    )
    assert unsafe_metrics["metricsLoopbackConfigured"] is False
    environment = domain_mcp.parse_cloudflared_service_metadata(
        "/usr/local/bin/cloudflared tunnel run",
        "OTHER=value TUNNEL_TOKEN=redacted",
    )
    assert environment["environmentTokenDetected"] is True

    unsafe_token = base / "tunnel-token"
    unsafe_token.write_text("redacted\n", encoding="utf-8")
    os.chmod(unsafe_token, 0o644)
    assert domain_mcp.root_token_file_safe(str(unsafe_token)) is False
    os.chmod(unsafe_token, 0o600)
    assert domain_mcp.root_token_file_safe(str(unsafe_token)) is False
    api_token = base / "cloudflare-api-token"
    api_token.write_text("t" * 43 + "\n", encoding="utf-8")
    os.chmod(api_token, 0o600)
    assert domain_mcp.read_private_token(api_token) == "t" * 43
    api_token_link = base / "cloudflare-api-token-link"
    api_token_link.symlink_to(api_token)
    try:
        domain_mcp.read_private_token(api_token_link)
    except domain_mcp.DomainMcpError:
        pass
    else:
        raise AssertionError("read_private_token accepted a symbolic link")
    try:
        domain_mcp.validate_redirect_uri(
            "https://chatgpt.com:8443/connector_platform_oauth_redirect"
        )
    except domain_mcp.DomainMcpError:
        pass
    else:
        raise AssertionError("validate_redirect_uri accepted a non-default port")
    assert (
        domain_mcp.validate_redirect_uri("https://chatgpt.com/connector/oauth/*")
        == "https://chatgpt.com/connector/oauth/*"
    )
    for unsafe_redirect in (
        "https://chatgpt.com/*",
        "https://chatgpt.com/connector/*",
        "https://chatgpt.com/connector/oauth/callback",
        "https://chatgpt.com.evil.example/connector/oauth/*",
    ):
        try:
            domain_mcp.validate_redirect_uri(unsafe_redirect)
        except domain_mcp.DomainMcpError:
            pass
        else:
            raise AssertionError(
                f"validate_redirect_uri accepted unsafe callback {unsafe_redirect!r}"
            )
    assert domain_mcp.validate_hostname("MCP.Example.COM.") == "mcp.example.com"
    try:
        domain_mcp.validate_hostname("mcp.exämple.com")
    except domain_mcp.DomainMcpError:
        pass
    else:
        raise AssertionError("validate_hostname accepted a non-ASCII hostname")

    class FakeResponse:
        status = 401
        headers = {
            "WWW-Authenticate": (
                'Bearer resource_metadata="https://mcp.example.com/'
                '.well-known/oauth-protected-resource"'
            )
        }

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeOpener:
        def open(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    preflight = domain_mcp.access_edge_preflight(config, opener=FakeOpener())
    assert preflight == {"ready": True, "status": 401, "oauthChallenge": True}

    origin_unit = domain_mcp.origin_unit(config, Path("/tmp/config.json"))
    gateway_unit = domain_mcp.gateway_unit(config, Path("/tmp/config.json"))
    expiry_timer = domain_mcp.expiry_timer_unit(config)
    assert "Restart=no" in origin_unit
    assert "RuntimeMaxSec=3600" in origin_unit
    assert "MemoryMax=2147483648" in origin_unit
    assert "MemorySwapMax=0" in origin_unit
    assert "CPUQuota=200%" in origin_unit
    assert "TasksMax=128" in origin_unit
    assert "LimitFSIZE=536870912" in origin_unit
    assert "WantedBy=default.target" not in origin_unit
    assert f"OnFailure={domain_mcp.EXPIRY_SERVICE_UNIT}" in origin_unit
    assert str(config["pythonExecutable"]) in origin_unit
    assert str(config["pythonExecutable"]) in gateway_unit
    assert f"Requires={domain_mcp.ORIGIN_UNIT} {domain_mcp.EXPIRY_TIMER_UNIT}" in gateway_unit
    assert f"OnFailure={domain_mcp.EXPIRY_SERVICE_UNIT}" in gateway_unit
    assert "MemoryMax=268435456" in gateway_unit
    assert "CPUQuota=50%" in gateway_unit
    assert "OnActiveSec=60m" in expiry_timer
    assert "ProtectSystem=" not in origin_unit
    assert "ProtectSystem=" not in gateway_unit
    assert {
        "python",
        "git",
        "manager",
        "agentMode",
        "advisorConcurrency",
        "advisorSafety",
        "readonlyPatch",
        "secureOriginPatch",
        "gateway",
        "secureServer",
        "shellSandbox",
    }.issubset(config["runtimeIntegrity"])
    devspace_dist = Path(config["devspaceDist"])
    expected_dist_integrity = {
        f"devspaceDist/{path.relative_to(devspace_dist).as_posix()}"
        for path in devspace_dist.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert expected_dist_integrity.issubset(config["runtimeIntegrity"])

    gateway_command = domain_mcp.gateway_bwrap_command(config)
    assert "--unshare-net" not in gateway_command
    assert str(config["projectDir"]) not in gateway_command
    assert "/run/advisor-gateway" in gateway_command
    assert "/run/advisor-origin" in gateway_command
    assert "/run/advisor-config/origin-secret" in gateway_command
    origin_command = domain_mcp.bwrap_command(config, command=["/bin/true"])
    shell_limit_index = origin_command.index("DEVSPACE_SHELL_MAX_SECONDS")
    assert origin_command[shell_limit_index + 1] == "3600"
    sync_shell_index = origin_command.index("DEVSPACE_DISABLE_SYNC_SHELL")
    assert origin_command[sync_shell_index + 1] == "true"
    process_limit_index = origin_command.index("DEVSPACE_PROCESS_MAX_ACTIVE")
    assert origin_command[process_limit_index + 1] == str(config["maxConcurrent"])

    assert domain_mcp.cloudflare_hardening_current(config) is False
    config["cloudflareHardening"] = {
        "profileVersion": domain_mcp.CLOUDFLARE_HARDENING_PROFILE,
        "verifiedAt": domain_mcp.datetime.now(domain_mcp.timezone.utc).isoformat(),
        "identityFingerprint": domain_mcp.identity_fingerprint(config),
        "redirectUriFingerprint": "a" * 64,
        "remoteIdentityFingerprint": "b" * 64,
        "connectorFingerprint": "c" * 64,
        "tunnelId": "11111111-2222-4333-8444-555555555555",
        "zoneId": "d" * 32,
    }
    assert domain_mcp.cloudflare_hardening_current(config) is True
    original_issuer = config["accessIssuer"]
    config["accessIssuer"] = "https://different.cloudflareaccess.com"
    assert domain_mcp.cloudflare_hardening_current(config) is False
    config["accessIssuer"] = original_issuer
    assert domain_mcp.cloudflare_hardening_current(config) is True
    original_git_state = config["gitStateFingerprint"]
    original_git_mutable = config["gitMutableFingerprint"]
    original_masks = list(config["sensitivePathMasks"])
    config["gitStateFingerprint"] = "e" * 64
    config["gitMutableFingerprint"] = "f" * 64
    config["sensitivePathMasks"] = [*original_masks, "new-secret-path"]
    assert domain_mcp.cloudflare_hardening_current(config) is True
    config["gitStateFingerprint"] = original_git_state
    config["gitMutableFingerprint"] = original_git_mutable
    config["sensitivePathMasks"] = original_masks
    original_project_inode = config["projectInode"]
    config["projectInode"] = original_project_inode + 1
    assert domain_mcp.cloudflare_hardening_current(config) is False
    config["projectInode"] = original_project_inode
    assert domain_mcp.cloudflare_hardening_current(config) is True
    stale = dict(config["cloudflareHardening"])
    stale["verifiedAt"] = (
        domain_mcp.datetime.now(domain_mcp.timezone.utc)
        - domain_mcp.timedelta(hours=domain_mcp.REMOTE_HARDENING_MAX_AGE_HOURS + 1)
    ).isoformat()
    config["cloudflareHardening"] = stale
    assert domain_mcp.cloudflare_hardening_current(config) is False
    config.pop("cloudflareHardening")

    guard_config = base / "guard-config.json"
    domain_mcp.atomic_json(guard_config, config)
    try:
        domain_mcp.start_services(guard_config)
    except domain_mcp.DomainMcpError as exc:
        assert "recent authenticated audit" in str(exc)
    else:
        raise AssertionError("start_services accepted an unaudited Cloudflare policy")

    gateway = subprocess.Popen(
        [
            str(Path(config["nodeRoot"]) / "bin" / "node"),
            str(config["gatewayScript"]),
            "--config",
            str(guard_config),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        gateway_socket = Path(config["gatewaySocket"])
        wait_for_socket(gateway_socket, gateway)
        assert domain_mcp.local_health(config) is True
        assert domain_mcp.socket_path_ready(gateway_socket) is True
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)

    sandboxed_gateway = subprocess.Popen(
        domain_mcp.gateway_bwrap_command(config),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        gateway_socket = Path(config["gatewaySocket"])
        wait_for_socket(gateway_socket, sandboxed_gateway)
        assert domain_mcp.local_health(config) is True
    finally:
        sandboxed_gateway.terminate()
        sandboxed_gateway.wait(timeout=10)

    assert domain_mcp.filesystem_capacity_healthy(
        [Path(config["projectDir"])],
        minimum_free_bytes=1,
        minimum_free_inodes=1,
    )
    assert not domain_mcp.filesystem_capacity_healthy(
        [Path(config["projectDir"])],
        minimum_free_bytes=2**63,
        minimum_free_inodes=1,
    )


def test_nested_shell_reuses_curated_etc_snapshot() -> None:
    assert shell_sandbox.sandbox_etc_arguments() == [
        "--ro-bind",
        "/etc",
        "/etc",
    ]
    source = (SCRIPTS / "devspace_shell_sandbox.py").read_text(encoding="utf-8")
    assert "/etc/ld.so.cache" not in source


def test_checkout_state_pin(base: Path) -> None:
    project = base / "checkout-pin-project"
    project.mkdir()
    (project / "tracked.txt").write_text("pinned\n", encoding="utf-8")
    (project / ".gitignore").write_text("cache/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "tracked.txt", ".gitignore"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Advisor Test",
            "-c",
            "user.email=advisor-test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    (project / "tracked.txt").write_text("dirty-a\n", encoding="utf-8")
    (project / "notes.txt").write_text("untracked-a\n", encoding="utf-8")
    (project / "cache").mkdir()
    (project / "cache" / "ignored.txt").write_text("ignored-a\n", encoding="utf-8")
    state = domain_mcp.git_checkout_state(project)
    config = {
        "projectDir": str(project),
        "gitExecutable": str(domain_mcp.require_executable("git")),
        "gitHead": state["head"],
        "gitStateFingerprint": state["fingerprint"],
        "gitMutableFingerprint": state["mutableFingerprint"],
        "gitMutableEntryCount": state["mutableEntryCount"],
        "dirtyCheckoutApproved": False,
        "sensitivePathMasks": domain_mcp.sensitive_mask_plan(project),
    }
    domain_mcp.verify_checkout_state(config)
    status_before = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain=v1"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    (project / "tracked.txt").write_text("dirty-b\n", encoding="utf-8")
    status_after = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain=v1"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert status_before == status_after
    try:
        domain_mcp.verify_checkout_state(config)
    except domain_mcp.DomainMcpError as exc:
        assert "changed after prepare" in str(exc)
    else:
        raise AssertionError("verify_checkout_state accepted a changed checkout")

    (project / "tracked.txt").write_text("dirty-a\n", encoding="utf-8")
    domain_mcp.verify_checkout_state(config)
    (project / "cache" / "ignored.txt").write_text("ignored-b\n", encoding="utf-8")
    domain_mcp.verify_checkout_state(config)

    (project / "notes.txt").write_text("untracked-b\n", encoding="utf-8")
    try:
        domain_mcp.verify_checkout_state(config)
    except domain_mcp.DomainMcpError as exc:
        assert "changed after prepare" in str(exc)
    else:
        raise AssertionError("verify_checkout_state accepted changed untracked content")

    git_config = project / ".git" / "config"
    original_git_config = git_config.read_text(encoding="utf-8")
    git_config.write_text(
        original_git_config + "\n[core]\n\tfsmonitor = /tmp/untrusted-helper\n",
        encoding="utf-8",
    )
    try:
        domain_mcp.git_checkout_state(
            project,
            domain_mcp.require_executable("git"),
        )
    except domain_mcp.DomainMcpError as exc:
        assert "Command-bearing or external-path Git config" in str(exc)
    else:
        raise AssertionError("checkout state accepted command-bearing Git config")
    finally:
        git_config.write_text(original_git_config, encoding="utf-8")


def test_checkout_boundary_guards(base: Path) -> None:
    project = base / "boundary-project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "regular.txt").write_text("regular\n", encoding="utf-8")
    clean_mountinfo = f"1 0 0:1 / {project} rw - ext4 /dev/test rw\n"
    domain_mcp.verify_exposed_tree_boundary(project, mountinfo_text=clean_mountinfo)

    nested = project / "nested"
    nested.mkdir()
    nested_mountinfo = (
        clean_mountinfo
        + f"2 1 0:2 / {nested} rw - tmpfs tmpfs rw\n"
    )
    try:
        domain_mcp.verify_exposed_tree_boundary(
            project,
            mountinfo_text=nested_mountinfo,
        )
    except domain_mcp.DomainMcpError as exc:
        assert "descendant mount point" in str(exc)
    else:
        raise AssertionError("boundary scan accepted a descendant mount")

    outside = base / "outside-hardlink-target"
    outside.write_text("outside\n", encoding="utf-8")
    hardlink = project / "hardlink"
    os.link(outside, hardlink)
    try:
        domain_mcp.verify_exposed_tree_boundary(
            project,
            mountinfo_text=clean_mountinfo,
        )
    except domain_mcp.DomainMcpError as exc:
        assert "hardlinked regular file" in str(exc)
    else:
        raise AssertionError("boundary scan accepted a hardlink")
    hardlink.unlink()

    local_python = project / "python"
    local_python.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(local_python, 0o755)
    try:
        domain_mcp.validate_runtime_executable(local_python, project, "Python")
    except domain_mcp.DomainMcpError as exc:
        assert "inside the exposed checkout" in str(exc)
    else:
        raise AssertionError("runtime validation accepted checkout-local Python")


def test_cloudflare_hardening_audit(config: dict[str, Any]) -> None:
    account_id = "a" * 32
    redirect_uri = "https://chatgpt.com/connector/oauth/*"
    app_id = "app-1234567890123456"
    idp_id = "idp-1234567890123456"
    tunnel_id = "11111111-2222-4333-8444-555555555555"
    connector_id = "22222222-3333-4444-8555-666666666666"
    zone_id = "b" * 32
    app = {
        "id": app_id,
        "type": "mcp",
        "domain": config["publicHostname"],
        "destinations": [{"type": "public", "uri": config["publicHostname"]}],
        "aud": config["accessAudience"],
        "session_duration": "15m",
        "allowed_idps": [idp_id],
        "auto_redirect_to_identity": True,
        "oauth_configuration": {
            "enabled": True,
            "dynamic_client_registration": {
                "enabled": True,
                "allow_any_on_localhost": False,
                "allow_any_on_loopback": False,
                "allowed_uris": [redirect_uri],
            },
            "grant": {
                "access_token_lifetime": "15m",
                "session_duration": "24h",
            },
        },
    }
    policy = {
        "decision": "allow",
        "include": [{"email": {"email": config["allowedEmails"][0]}}],
        "exclude": [],
        "require": [{"cloudflare_account_member": {"account_id": account_id}}],
        "mfa_config": {
            "mfa_disabled": False,
            "allowed_authenticators": ["security_key", "biometrics"],
            "session_duration": "24h",
        },
    }
    idp = {
        "id": idp_id,
        "type": "cloudflare",
        "config": {"restrict_to_account_members": True},
    }

    class JsonResponse:
        status = 200

        def __init__(
            self,
            result: Any,
            *,
            result_info: dict[str, int] | None = None,
        ) -> None:
            self.result = result
            self.result_info = result_info

        def __enter__(self) -> "JsonResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            payload: dict[str, Any] = {"success": True, "result": self.result}
            if self.result_info is not None:
                payload["result_info"] = self.result_info
            return json.dumps(payload).encode()

    class CloudflareOpener:
        def __init__(
            self,
            *,
            localhost_allowed: bool = False,
            weak_authenticator_allowed: bool = False,
            tcp_origin: bool = False,
            omit_config_account_id: bool = False,
            multiple_connectors: bool = False,
            omit_result_info: bool = False,
            long_policy_session: bool = False,
            omit_team_name: bool = False,
            wrong_team_name: bool = False,
        ) -> None:
            self.localhost_allowed = localhost_allowed
            self.weak_authenticator_allowed = weak_authenticator_allowed
            self.tcp_origin = tcp_origin
            self.omit_config_account_id = omit_config_account_id
            self.multiple_connectors = multiple_connectors
            self.omit_result_info = omit_result_info
            self.long_policy_session = long_policy_session
            self.omit_team_name = omit_team_name
            self.wrong_team_name = wrong_team_name

        def list_response(
            self,
            request_path: str,
            items: list[Any],
            *,
            force_one_per_page: bool = False,
        ) -> JsonResponse:
            query = domain_mcp.urllib.parse.parse_qs(
                domain_mcp.urllib.parse.urlsplit(request_path).query
            )
            page = int(query["page"][0])
            requested_per_page = int(query["per_page"][0])
            per_page = 1 if force_one_per_page else requested_per_page
            start = (page - 1) * per_page
            page_items = items[start : start + per_page]
            info = {
                "page": page,
                "per_page": per_page,
                "count": len(page_items),
                "total_count": len(items),
            }
            return JsonResponse(
                page_items,
                result_info=None if self.omit_result_info else info,
            )

        def open(self, request: Any, **_kwargs: Any) -> JsonResponse:
            assert request.headers["Authorization"] == "Bearer " + "t" * 43
            path = request.full_url.removeprefix(domain_mcp.CLOUDFLARE_API_ROOT)
            endpoint = domain_mcp.urllib.parse.urlsplit(path).path
            if endpoint.endswith("/access/organizations"):
                return JsonResponse(
                    {"auth_domain": config["accessIssuer"].removeprefix("https://")}
                )
            if endpoint.endswith(f"/cfd_tunnel/{tunnel_id}"):
                return JsonResponse(
                    {
                        "id": tunnel_id,
                        "account_tag": account_id,
                        "config_src": "cloudflare",
                    }
                )
            if endpoint.endswith(f"/cfd_tunnel/{tunnel_id}/configurations"):
                service = (
                    "http://127.0.0.1:7676"
                    if self.tcp_origin
                    else f"unix:{config['gatewaySocket']}"
                )
                result = {
                    "tunnel_id": tunnel_id,
                    "source": "cloudflare",
                    "config": {
                        "ingress": [
                            {
                                "hostname": config["publicHostname"],
                                "service": service,
                                "originRequest": {
                                    "access": {
                                        "required": True,
                                        "audTag": [config["accessAudience"]],
                                        "teamName": (
                                            "wrong-team"
                                            if self.wrong_team_name
                                            else config["accessIssuer"]
                                            .removeprefix("https://")
                                            .removesuffix(".cloudflareaccess.com")
                                        ),
                                    }
                                },
                            },
                            {"service": "http_status:404"},
                        ]
                    },
                }
                if self.omit_team_name:
                    del result["config"]["ingress"][0]["originRequest"]["access"][
                        "teamName"
                    ]
                if not self.omit_config_account_id:
                    result["account_id"] = account_id
                return JsonResponse(result)
            if endpoint.endswith(f"/cfd_tunnel/{tunnel_id}/connections"):
                connector_ids = [connector_id]
                if self.multiple_connectors:
                    connector_ids.append("33333333-4444-4555-8666-777777777777")
                return JsonResponse(
                    [
                        {
                            "id": item_id,
                            "conns": [
                                {
                                    "client_id": item_id,
                                    "is_pending_reconnect": False,
                                }
                            ],
                        }
                        for item_id in connector_ids
                    ],
                )
            if endpoint.endswith(f"/zones/{zone_id}"):
                return JsonResponse(
                    {
                        "id": zone_id,
                        "name": "example.com",
                        "status": "active",
                        "account": {"id": account_id},
                    }
                )
            if endpoint == f"/zones/{zone_id}/dns_records":
                return self.list_response(
                    path,
                    [
                        {
                            "id": "c" * 32,
                            "type": "CNAME",
                            "name": config["publicHostname"],
                            "content": f"{tunnel_id}.cfargotunnel.com",
                            "proxied": True,
                        }
                    ]
                )
            if endpoint.endswith("/access/apps"):
                return self.list_response(path, [app])
            if endpoint.endswith(f"/access/apps/{app_id}"):
                detail = json.loads(json.dumps(app))
                detail["oauth_configuration"]["dynamic_client_registration"][
                    "allow_any_on_localhost"
                ] = self.localhost_allowed
                return JsonResponse(detail)
            if endpoint.endswith(f"/access/apps/{app_id}/policies"):
                detail = json.loads(json.dumps(policy))
                if self.weak_authenticator_allowed:
                    detail["mfa_config"]["allowed_authenticators"].append("totp")
                if self.long_policy_session:
                    detail["session_duration"] = "24h"
                return self.list_response(path, [detail])
            if endpoint.endswith("/access/identity_providers"):
                return self.list_response(path, [idp])
            raise AssertionError(f"Unexpected Cloudflare API path: {path}")

    local_connector = {
        "tunnelId": tunnel_id,
        "connectorId": connector_id,
        "active": True,
    }
    checks = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(),
    )
    assert checks["ready"] is True
    assert checks["finalCatchAllDeny"] is True
    assert checks["proxiedDnsRouteExact"] is True
    assert checks["singleActiveConnector"] is True
    assert checks["localTunnelIdentityExact"] is True
    assert checks["localConnectorIdentityExact"] is True
    assert checks["shortEffectivePolicySession"] is True
    assert checks["tunnelAccessTeamExact"] is True
    optional_account_id_omitted = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(omit_config_account_id=True),
    )
    assert optional_account_id_omitted["ready"] is True
    weakened = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(localhost_allowed=True),
    )
    assert weakened["ready"] is False
    assert weakened["dynamicRegistrationRestricted"] is False
    weak_mfa = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(weak_authenticator_allowed=True),
    )
    assert weak_mfa["ready"] is False
    assert weak_mfa["phishingResistantMfaRequired"] is False
    long_policy_session = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(long_policy_session=True),
    )
    assert long_policy_session["ready"] is False
    assert long_policy_session["shortEffectivePolicySession"] is False
    missing_team_name = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(omit_team_name=True),
    )
    assert missing_team_name["ready"] is False
    assert missing_team_name["tunnelAccessTeamExact"] is False
    wrong_team_name = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(wrong_team_name=True),
    )
    assert wrong_team_name["ready"] is False
    assert wrong_team_name["tunnelAccessTeamExact"] is False
    tcp_origin = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(tcp_origin=True),
    )
    assert tcp_origin["ready"] is False
    assert tcp_origin["privateUnixOriginExact"] is False
    wrong_local_tunnel = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity="33333333-4444-4555-8666-777777777777",
        local_connector_identity=local_connector,
        opener=CloudflareOpener(),
    )
    assert wrong_local_tunnel["ready"] is False
    assert wrong_local_tunnel["localTunnelIdentityExact"] is False

    wrong_local_connector = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity={
            "tunnelId": tunnel_id,
            "connectorId": "33333333-4444-4555-8666-777777777777",
            "active": True,
        },
        opener=CloudflareOpener(),
    )
    assert wrong_local_connector["ready"] is False
    assert wrong_local_connector["localConnectorIdentityExact"] is False

    multiple_connectors = domain_mcp.audit_cloudflare_hardening(
        config,
        account_id=account_id,
        token="t" * 43,
        redirect_uri=redirect_uri,
        tunnel_id=tunnel_id,
        zone_id=zone_id,
        local_tunnel_identity=tunnel_id,
        local_connector_identity=local_connector,
        opener=CloudflareOpener(multiple_connectors=True),
    )
    assert multiple_connectors["ready"] is False
    assert multiple_connectors["singleActiveConnector"] is False

    try:
        domain_mcp.audit_cloudflare_hardening(
            config,
            account_id=account_id,
            token="t" * 43,
            redirect_uri=redirect_uri,
            tunnel_id=tunnel_id,
            zone_id=zone_id,
            local_tunnel_identity=tunnel_id,
            local_connector_identity=local_connector,
            opener=CloudflareOpener(omit_result_info=True),
        )
    except domain_mcp.DomainMcpError as exc:
        assert "paginated inventory metadata" in str(exc)
    else:
        raise AssertionError("Cloudflare audit accepted missing pagination metadata")


def test_local_cloudflared_diagnostics() -> None:
    tunnel_id = "11111111-2222-4333-8444-555555555555"
    connector_id = "22222222-3333-4444-8555-666666666666"

    class DiagnosticResponse:
        status = 200

        def __enter__(self) -> "DiagnosticResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {
                    "tunnelID": tunnel_id,
                    "connectorID": connector_id,
                    "connections": [{"isConnected": True}],
                }
            ).encode()

    class DiagnosticOpener:
        def open(self, request: Any, **_kwargs: Any) -> DiagnosticResponse:
            assert request.full_url == "http://127.0.0.1:60123/diag/tunnel"
            return DiagnosticResponse()

    identity = domain_mcp.local_cloudflared_identity(
        "127.0.0.1:60123",
        opener=DiagnosticOpener(),
    )
    assert identity == {
        "tunnelId": tunnel_id,
        "connectorId": connector_id,
        "active": True,
    }
    try:
        domain_mcp.local_cloudflared_identity(
            "0.0.0.0:60123",
            opener=DiagnosticOpener(),
        )
    except domain_mcp.DomainMcpError as exc:
        assert "loopback metrics port" in str(exc)
    else:
        raise AssertionError("local diagnostics accepted a non-loopback address")


def test_sandbox(config: dict[str, Any], project: Path, base: Path) -> None:
    outside = base / "outside-secret"
    outside.write_text("must stay hidden\n", encoding="utf-8")
    advisor_state = project / ".codex-advisor"
    advisor_state.mkdir()
    (advisor_state / "private-transcript.md").write_text("hidden\n", encoding="utf-8")
    config["sensitivePathMasks"] = sorted(
        set(config["sensitivePathMasks"]) | {".codex-advisor"}
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host_port = listener.getsockname()[1]
    probe = r"""
import json
import os
from pathlib import Path
import socket

network_connected = False
try:
    client = socket.create_connection(("127.0.0.1", int(os.environ["TEST_HOST_PORT"])), timeout=1)
    client.close()
    network_connected = True
except OSError:
    pass
Path("/workspace/sandbox-write.txt").write_text("propagated\n", encoding="utf-8")
print(json.dumps({
    "cwd": os.getcwd(),
    "home_hidden": sorted(path.name for path in Path("/home").iterdir()) == ["devspace"],
    "outside_hidden": not Path(os.environ["TEST_OUTSIDE_PATH"]).exists(),
    "advisor_state_masked": not Path("/workspace/.codex-advisor/private-transcript.md").exists(),
    "sentinel_env_hidden": "ADVISOR_TEST_SENTINEL" not in os.environ,
    "network_connected": network_connected,
}))
"""
    command = domain_mcp.bwrap_command(
        config,
        command=["/usr/bin/python3", "-c", probe],
    )
    insertion = command.index("--hostname")
    command[insertion:insertion] = [
        "--setenv",
        "TEST_HOST_PORT",
        str(host_port),
        "--setenv",
        "TEST_OUTSIDE_PATH",
        str(outside),
    ]
    environment = os.environ.copy()
    environment["ADVISOR_TEST_SENTINEL"] = "must-not-cross-clearenv"
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=20,
        env=environment,
        check=False,
    )
    listener.close()
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip())
    assert report == {
        "cwd": "/workspace",
        "home_hidden": True,
        "outside_hidden": True,
        "advisor_state_masked": True,
        "sentinel_env_hidden": True,
        "network_connected": False,
    }
    assert (project / "sandbox-write.txt").read_text(encoding="utf-8") == "propagated\n"


def test_mcp(config: dict[str, Any], project: Path, secret: str) -> None:
    socket_path = Path(config["originSocket"])
    staged_secret = domain_mcp.stage_runtime_secret(config)
    process = subprocess.Popen(
        domain_mcp.bwrap_command(config),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_socket(socket_path, process)
        assert not staged_secret.exists()
        unauthorized_status, _, _ = unix_request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "security-test", "version": "1"},
                },
            },
            secret=None,
        )
        assert unauthorized_status == 401

        status, headers, body = unix_request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "security-test", "version": "1"},
                },
            },
            secret=secret,
        )
        assert status == 200, body.decode("utf-8", errors="replace")
        initialized = mcp_payload(body)
        assert initialized.get("result", {}).get("serverInfo", {}).get("name") == "devspace"
        session_id = headers.get("mcp-session-id")
        assert session_id

        status, _, body = unix_request(
            socket_path,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            secret=secret,
            session_id=session_id,
        )
        assert status == 200
        listed_tools = mcp_payload(body)["result"]["tools"]
        tools = {tool["name"] for tool in listed_tools}
        expected_tools = {
            "open_workspace",
            "read",
            "write",
            "edit",
            "grep",
            "glob",
            "ls",
            "exec_command",
            "write_stdin",
        }
        assert tools == expected_tools, {
            "missing": sorted(expected_tools - tools),
            "unexpected": sorted(tools - expected_tools),
        }
        exec_tool = next(tool for tool in listed_tools if tool["name"] == "exec_command")
        exec_properties = exec_tool["inputSchema"]["properties"]
        assert "executionKey" in exec_properties
        assert "allowConcurrentDuplicate" in exec_properties
        assert exec_properties["yieldTimeMs"]["maximum"] == 30_000

        pointer = Path(config["pinnedRootFile"])
        os.chmod(pointer, 0o644)
        try:
            unsafe_pointer = tool_call(
                socket_path,
                secret,
                session_id,
                4,
                "open_workspace",
                {"path": "/workspace", "mode": "checkout"},
            )
        finally:
            os.chmod(pointer, 0o600)
        assert unsafe_pointer["result"].get("isError") is True
        assert "must not be group- or world-accessible" in json.dumps(unsafe_pointer)

        opened = tool_call(
            socket_path,
            secret,
            session_id,
            5,
            "open_workspace",
            {"path": "/workspace", "mode": "checkout"},
        )
        workspace_id = opened["result"]["structuredContent"]["workspaceId"]

        denied = tool_call(
            socket_path,
            secret,
            session_id,
            6,
            "open_workspace",
            {"path": "/state", "mode": "checkout"},
        )
        assert denied["result"].get("isError") is True

        written = tool_call(
            socket_path,
            secret,
            session_id,
            7,
            "write",
            {
                "workspaceId": workspace_id,
                "path": "agent-created.txt",
                "content": "written through MCP\n",
            },
        )
        assert written["result"].get("isError") is not True
        assert (project / "agent-created.txt").read_text(encoding="utf-8") == "written through MCP\n"

        shell = tool_call(
            socket_path,
            secret,
            session_id,
            8,
            "exec_command",
            {
                "workspaceId": workspace_id,
                "cmd": (
                    "pwd && test \"$(ls -A /home)\" = devspace "
                    "&& test ! -e /home/devspace/.ssh "
                    "&& test ! -e /run/advisor-origin "
                    "&& test ! -e /run/advisor-gateway "
                    "&& test ! -e /state "
                    "&& test ! -e /workspace/.codex-advisor/private-transcript.md "
                    "&& test ! -s /workspace/.git/config "
                    "&& test ! -w /workspace/.git "
                    "&& ! env | cut -d= -f1 | grep -q '^DEVSPACE_' "
                    "&& ! env | cut -d= -f1 | grep -Eiq "
                    "'(TOKEN|SECRET|COOKIE|PRIVATE_KEY|API_KEY|AUTH)' "
                    "&& ln -s /run/advisor-origin runtime-link "
                    "&& printf shell-write > shell-created.txt "
                    "&& printf sandbox-ok"
                ),
                "yieldTimeMs": 30_000,
            },
        )
        result_text = shell["result"]["structuredContent"]["result"]
        assert "/workspace" in result_text and "sandbox-ok" in result_text
        assert (project / "shell-created.txt").read_text(encoding="utf-8") == "shell-write"

        long_command = (
            "printf 'automatic\\n' >> replay-count.txt; "
            "sleep 2; printf async-complete"
        )
        command_started = time.monotonic()
        first_execution = tool_call(
            socket_path,
            secret,
            session_id,
            9,
            "exec_command",
            {
                "workspaceId": workspace_id,
                "cmd": long_command,
                "yieldTimeMs": 0,
            },
        )
        first_elapsed = time.monotonic() - command_started
        first_process = first_execution["result"]["structuredContent"]
        assert first_elapsed < 1.5
        assert first_process["running"] is True
        assert first_process["reused"] is False
        assert first_process["workspaceId"] == workspace_id
        process_session_id = first_process["sessionId"]

        duplicate_execution = tool_call(
            socket_path,
            secret,
            session_id,
            10,
            "exec_command",
            {
                "workspaceId": workspace_id,
                "cmd": long_command,
                "yieldTimeMs": 0,
            },
        )
        duplicate_process = duplicate_execution["result"]["structuredContent"]
        assert duplicate_process["reused"] is True
        assert duplicate_process["sessionId"] == process_session_id

        second_status, second_headers, second_body = unix_request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "reconnect-test", "version": "1"},
                },
            },
            secret=secret,
        )
        assert second_status == 200, second_body.decode("utf-8", errors="replace")
        second_session_id = second_headers.get("mcp-session-id")
        assert second_session_id
        second_opened = tool_call(
            socket_path,
            secret,
            second_session_id,
            12,
            "open_workspace",
            {"path": "/workspace", "mode": "checkout"},
        )
        second_workspace_id = second_opened["result"]["structuredContent"]["workspaceId"]
        reconnect_execution = tool_call(
            socket_path,
            secret,
            second_session_id,
            13,
            "exec_command",
            {
                "workspaceId": second_workspace_id,
                "cmd": long_command,
                "yieldTimeMs": 0,
            },
        )
        reconnect_process = reconnect_execution["result"]["structuredContent"]
        assert reconnect_process["reused"] is True
        assert reconnect_process["sessionId"] == process_session_id
        assert reconnect_process["workspaceId"] == workspace_id

        completed_execution = tool_call(
            socket_path,
            secret,
            second_session_id,
            14,
            "write_stdin",
            {
                "workspaceId": second_workspace_id,
                "sessionId": process_session_id,
                "yieldTimeMs": 5_000,
            },
        )
        completed_process = completed_execution["result"]["structuredContent"]
        assert completed_process["running"] is False
        assert completed_process["exitCode"] == 0
        assert "async-complete" in completed_process["result"]
        assert (project / "replay-count.txt").read_text(encoding="utf-8").splitlines() == [
            "automatic"
        ]

        completed_replay = tool_call(
            socket_path,
            secret,
            second_session_id,
            15,
            "exec_command",
            {
                "workspaceId": second_workspace_id,
                "cmd": long_command,
                "yieldTimeMs": 0,
            },
        )
        completed_replay_process = completed_replay["result"]["structuredContent"]
        assert completed_replay_process["reused"] is True
        assert completed_replay_process["running"] is False
        assert completed_replay_process["sessionId"] == process_session_id
        assert "async-complete" in completed_replay_process["result"]

        intentional_command = (
            "printf 'intentional\\n' >> intentional-count.txt; "
            "sleep 1; printf intentional-complete"
        )
        intentional_sessions: list[int] = []
        for request_id, execution_key in ((16, "parallel-a"), (17, "parallel-b")):
            intentional = tool_call(
                socket_path,
                secret,
                second_session_id,
                request_id,
                "exec_command",
                {
                    "workspaceId": second_workspace_id,
                    "cmd": intentional_command,
                    "executionKey": execution_key,
                    "yieldTimeMs": 0,
                },
            )
            intentional_process = intentional["result"]["structuredContent"]
            assert intentional_process["reused"] is False
            assert intentional_process["running"] is True
            intentional_sessions.append(intentional_process["sessionId"])
        assert len(set(intentional_sessions)) == 2
        for request_id, intentional_session in zip(
            (18, 19),
            intentional_sessions,
            strict=True,
        ):
            intentional_complete = tool_call(
                socket_path,
                secret,
                second_session_id,
                request_id,
                "write_stdin",
                {
                    "workspaceId": second_workspace_id,
                    "sessionId": intentional_session,
                    "yieldTimeMs": 5_000,
                },
            )
            assert (
                intentional_complete["result"]["structuredContent"]["running"]
                is False
            )
        assert (
            project / "intentional-count.txt"
        ).read_text(encoding="utf-8").splitlines() == [
            "intentional",
            "intentional",
        ]

        collision = tool_call(
            socket_path,
            secret,
            second_session_id,
            20,
            "exec_command",
            {
                "workspaceId": second_workspace_id,
                "cmd": "printf must-not-run",
                "executionKey": "parallel-a",
                "yieldTimeMs": 0,
            },
        )
        assert collision["result"].get("isError") is True
        assert "already bound to a different command request" in json.dumps(collision)

        override_command = (
            "printf 'override\\n' >> override-count.txt; "
            "sleep 1; printf override-complete"
        )
        protected = tool_call(
            socket_path,
            secret,
            second_session_id,
            21,
            "exec_command",
            {
                "workspaceId": second_workspace_id,
                "cmd": override_command,
                "yieldTimeMs": 0,
            },
        )["result"]["structuredContent"]
        overridden = tool_call(
            socket_path,
            secret,
            second_session_id,
            22,
            "exec_command",
            {
                "workspaceId": second_workspace_id,
                "cmd": override_command,
                "allowConcurrentDuplicate": True,
                "yieldTimeMs": 0,
            },
        )["result"]["structuredContent"]
        assert protected["sessionId"] != overridden["sessionId"]
        assert protected["reused"] is False
        assert overridden["reused"] is False
        for request_id, override_session in (
            (23, protected["sessionId"]),
            (24, overridden["sessionId"]),
        ):
            override_complete = tool_call(
                socket_path,
                secret,
                second_session_id,
                request_id,
                "write_stdin",
                {
                    "workspaceId": second_workspace_id,
                    "sessionId": override_session,
                    "yieldTimeMs": 5_000,
                },
            )
            assert override_complete["result"]["structuredContent"]["running"] is False
        assert (project / "override-count.txt").read_text(encoding="utf-8").splitlines() == [
            "override",
            "override",
        ]

        limited_sessions: list[int] = []
        for offset in range(config["maxConcurrent"]):
            limited = tool_call(
                socket_path,
                secret,
                second_session_id,
                25 + offset,
                "exec_command",
                {
                    "workspaceId": second_workspace_id,
                    "cmd": f"sleep 2; printf limit-{offset}",
                    "executionKey": f"limit-{offset}",
                    "yieldTimeMs": 0,
                },
            )
            limited_process = limited["result"]["structuredContent"]
            assert limited_process["running"] is True
            limited_sessions.append(limited_process["sessionId"])
        overflow = tool_call(
            socket_path,
            secret,
            second_session_id,
            25 + config["maxConcurrent"],
            "exec_command",
            {
                "workspaceId": second_workspace_id,
                "cmd": "sleep 2; printf overflow",
                "executionKey": "limit-overflow",
                "yieldTimeMs": 0,
            },
        )
        assert overflow["result"].get("isError") is True
        assert "Active process-session limit reached" in json.dumps(overflow)
        poll_request = 26 + config["maxConcurrent"]
        for limited_session in limited_sessions:
            for _ in range(3):
                limited_complete = tool_call(
                    socket_path,
                    secret,
                    second_session_id,
                    poll_request,
                    "write_stdin",
                    {
                        "workspaceId": second_workspace_id,
                        "sessionId": limited_session,
                        "yieldTimeMs": 5_000,
                    },
                )
                poll_request += 1
                if not limited_complete["result"]["structuredContent"]["running"]:
                    break
            assert limited_complete["result"]["structuredContent"]["running"] is False

        symlink_list = tool_call(
            socket_path,
            secret,
            session_id,
            poll_request,
            "ls",
            {"workspaceId": workspace_id, "path": "runtime-link"},
        )
        assert symlink_list["result"].get("isError") is True
        symlink_write = tool_call(
            socket_path,
            secret,
            session_id,
            poll_request + 1,
            "write",
            {
                "workspaceId": workspace_id,
                "path": "runtime-link/injected",
                "content": "must not reach the private runtime\n",
            },
        )
        assert symlink_write["result"].get("isError") is True
        assert not (Path(config["originRuntimeDir"]) / "injected").exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    domain_mcp.patch_devspace(domain_mcp.require_executable("devspace"), check=True)
    test_cloudflared_runtime_namespace_contract()
    test_nested_shell_reuses_curated_etc_snapshot()
    with tempfile.TemporaryDirectory(prefix="advisor-domain-mcp-test-") as temporary:
        base = Path(temporary)
        config, project, secret = fixture_config(base)
        test_expiry_timer_status(config)
        test_manager_guards(config, base)
        test_checkout_state_pin(base)
        test_checkout_boundary_guards(base)
        test_cloudflare_hardening_audit(config)
        test_local_cloudflared_diagnostics()
        test_sandbox(config, project, base)
        test_mcp(config, project, secret)
    print("Advisor domain MCP sandbox and integration tests passed.")


if __name__ == "__main__":
    main()
