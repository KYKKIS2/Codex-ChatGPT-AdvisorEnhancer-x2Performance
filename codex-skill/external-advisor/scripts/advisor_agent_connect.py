#!/usr/bin/env python3
"""Prepare and launch repo-aware advisor agent-mode for ChatGPT.

This command automates the local side only: root validation, sanitized review
workspace creation, user-level allowed-root config, optional DevSpace serve
startup, and the exact ChatGPT connector URL/handoff text. It never modifies
ChatGPT account settings.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

import advisor_concurrency as concurrency
import advisor_safety as safety
import agent_mode


PUBLIC_BASE_URL_ENV = "ADVISOR_AGENT_PUBLIC_BASE_URL"
RUNTIME_ROOT_ENV = "ADVISOR_AGENT_RUNTIME_ROOT"
DEFAULT_CONNECT_TIMEOUT = 30
HTTPS_URL_RE = re.compile(r"https://[^\s\"'<>]+")
QUICK_TUNNEL_URL_RE = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com(?:/[^\s\"'<>]*)?")
VALID_TUNNEL_MODES = {"auto", "configured", "off"}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_project(path: Path) -> Path:
    return agent_mode.resolve_path(path)


def runtime_root(raw: str | Path | None = None) -> Path:
    configured = raw or os.environ.get(RUNTIME_ROOT_ENV)
    root = Path(configured).expanduser().resolve() if configured else agent_mode.codex_home() / "advisor-agent" / "devspace"
    safety.ensure_private_dir(root)
    return root


def project_runtime_slug(project: Path) -> str:
    return agent_mode.sanitized_workspace_slug(project)


def state_paths(project: Path, root: Path) -> dict[str, Path]:
    slug = project_runtime_slug(project)
    project_dir = root / slug
    safety.ensure_private_dir(project_dir)
    return {
        "dir": project_dir,
        "state": project_dir / "state.json",
        "log": project_dir / "devspace.log",
        "tunnel_log": project_dir / "cloudflared.log",
        "exact_root": project_dir / "readonly-exact-root.txt",
        "lock": project_dir / "lifecycle.lock",
    }


def write_exact_root(path: Path, workspace: Path) -> None:
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise RuntimeError("The pinned advisor workspace does not exist.")
    safety.atomic_write_text(path, str(resolved) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def exact_root_matches(path: Path, workspace: Path) -> bool:
    try:
        recorded = Path(path.read_text(encoding="utf-8").strip()).expanduser().resolve()
        return recorded == workspace.expanduser().resolve() and recorded.is_dir()
    except (OSError, ValueError):
        return False


def path_is_sensitive_for_display(path: Path) -> bool:
    return agent_mode.contains_sensitive_agent_marker(path)


def normalize_public_url(raw: str) -> tuple[str, str]:
    value = raw.strip().strip("`'\"()[]{}<>,.;")
    if not value:
        raise ValueError("empty public URL")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("ChatGPT connector URL must use https")
    if not parsed.netloc:
        raise ValueError("ChatGPT connector URL must include a host")
    host = parsed.hostname or ""
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("ChatGPT connector URL cannot use localhost or .local hosts")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified):
        raise ValueError("ChatGPT connector URL cannot use private, loopback, or link-local IPs")
    base_path = parsed.path.rstrip("/")
    if base_path.endswith("/mcp") or base_path == "/mcp":
        mcp_path = base_path
        base_path = base_path[: -len("/mcp")] or ""
    else:
        mcp_path = f"{base_path}/mcp" if base_path else "/mcp"
    base = urlunparse((parsed.scheme, parsed.netloc, base_path, "", "", ""))
    mcp = urlunparse((parsed.scheme, parsed.netloc, mcp_path, "", "", ""))
    return base.rstrip("/"), mcp


def find_public_url_in_text(text: str) -> tuple[str, str] | None:
    for match in HTTPS_URL_RE.finditer(text):
        candidate = match.group(0)
        try:
            return normalize_public_url(candidate)
        except ValueError:
            continue
    return None


def run_small_command(command: list[str], *, cwd: Path, timeout: int = 5) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    return completed.returncode, safety.redact_sensitive_text(output)


def discover_public_url(args: argparse.Namespace, bridge_path: str, cwd: Path) -> tuple[str, str, str]:
    candidates: list[tuple[str, str]] = []
    if getattr(args, "public_base_url", None):
        candidates.append(("--public-base-url", args.public_base_url))
    for env_name in (PUBLIC_BASE_URL_ENV, "DEVSPACE_PUBLIC_BASE_URL"):
        if os.environ.get(env_name):
            candidates.append((env_name, os.environ[env_name]))
    for source, value in candidates:
        try:
            base, mcp = normalize_public_url(value)
            return base, mcp, source
        except ValueError as exc:
            raise RuntimeError(f"{source} is not a valid ChatGPT public URL: {exc}") from exc

    code, output = run_small_command([bridge_path, "config", "get", "publicBaseUrl"], cwd=cwd, timeout=5)
    if code == 0 and output and output.strip().lower() not in {"null", "undefined", "none"}:
        found = find_public_url_in_text(output)
        if found:
            base, mcp = found
            return base, mcp, "devspace config publicBaseUrl"
    return "", "", ""


def read_devspace_runtime(bridge_path: str, cwd: Path) -> dict[str, Any]:
    code, output = run_small_command([bridge_path, "config", "get"], cwd=cwd, timeout=5)
    config: dict[str, Any] = {}
    if code == 0 and output:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            config = parsed
    # The public Cloudflare endpoint is the only intended remote surface.
    # Ignore inherited/configured listener hosts and keep DevSpace on loopback.
    host = "127.0.0.1"
    try:
        port = int(os.environ.get("PORT") or config.get("port") or 7676)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("DevSpace port must be an integer.") from exc
    if port < 1 or port > 65535:
        raise RuntimeError("DevSpace port must be between 1 and 65535.")
    formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return {
        "host": host,
        "port": port,
        "origin": f"http://{formatted_host}:{port}",
        "mcp_url": f"http://{formatted_host}:{port}/mcp",
    }


def probe_mcp_url(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ready": False,
        "status": 0,
        "oauth_challenge": False,
        "checked_utc": utc_now(),
        "error": "",
    }
    request = Request(
        url,
        method="GET",
        headers={"Accept": "application/json, text/event-stream"},
    )
    try:
        response = (
            concurrency.open_loopback_url(request, timeout=timeout)
            if concurrency.loopback_url_candidate(url)
            else urlopen(request, timeout=timeout)
        )
        with response:
            result["status"] = int(getattr(response, "status", 0) or 0)
            challenge = str(response.headers.get("WWW-Authenticate") or "")
            result["oauth_challenge"] = "bearer" in challenge.lower()
    except HTTPError as exc:
        result["status"] = int(exc.code)
        challenge = str(exc.headers.get("WWW-Authenticate") or "")
        result["oauth_challenge"] = "bearer" in challenge.lower()
        if exc.code != 401:
            result["error"] = f"HTTP {exc.code}"
    except (URLError, OSError, RuntimeError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        result["error"] = safety.truncate(safety.redact_sensitive_text(str(reason)), 240)
        return result
    result["ready"] = result["status"] == 401 and result["oauth_challenge"]
    if not result["ready"] and not result["error"]:
        result["error"] = "endpoint did not return the expected OAuth Bearer challenge"
    return result


def wait_for_mcp_readiness(
    *,
    local_url: str,
    public_url: str,
    processes: list[subprocess.Popen[Any]],
    timeout: int,
    skip_public_probe: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.time() + timeout
    local_probe: dict[str, Any] = {}
    public_probe: dict[str, Any] = {}
    while time.time() < deadline:
        if any(proc.poll() is not None for proc in processes):
            break
        local_probe = probe_mcp_url(local_url, timeout=2.0)
        if skip_public_probe:
            public_probe = {
                "ready": True,
                "status": 0,
                "oauth_challenge": False,
                "checked_utc": utc_now(),
                "error": "",
                "skipped": True,
            }
        elif local_probe["ready"]:
            public_probe = probe_mcp_url(public_url, timeout=4.0)
        if local_probe.get("ready") and public_probe.get("ready"):
            return local_probe, public_probe
        time.sleep(0.5)
    return local_probe or probe_mcp_url(local_url, timeout=2.0), public_probe or (
        {
            "ready": True,
            "status": 0,
            "oauth_challenge": False,
            "checked_utc": utc_now(),
            "error": "",
            "skipped": True,
        }
        if skip_public_probe
        else probe_mcp_url(public_url, timeout=4.0)
    )


def configure_allowed_roots(args: argparse.Namespace, *, dry_run: bool = False) -> dict[str, Any]:
    project = resolve_project(args.project_dir)
    allowed_root = agent_mode.resolve_path(args.allowed_root) if args.allowed_root else project
    config_path = agent_mode.config_path(args.config_path)
    case_insensitive = args.case_insensitive_paths or agent_mode.default_case_insensitive()
    errors: list[str] = []
    warnings: list[str] = []

    root_result = agent_mode.validate_project_under_allowed_root(project, allowed_root, case_insensitive=case_insensitive)
    errors.extend(root_result.errors)
    warnings.extend(root_result.warnings)
    if agent_mode.path_is_same_or_child(config_path, project, case_insensitive=case_insensitive):
        errors.append("agent config path must live outside the project being exposed")

    scan = agent_mode.scan_project_secrets(project, allow_sensitive_project=args.allow_sensitive_project)
    warnings.extend(scan.warnings)
    sanitized = None
    sanitized_mode = (args.sanitized_workspace or agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE).strip().lower()
    if sanitized_mode not in agent_mode.VALID_SANITIZED_WORKSPACE_MODES:
        sanitized_mode = agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE
    needs_sanitized = (not scan.ok or sanitized_mode == "always") and sanitized_mode != "off" and not args.allow_sensitive_project
    if needs_sanitized and not dry_run:
        sanitized = agent_mode.create_sanitized_workspace(
            project,
            mode=sanitized_mode,
            workspace_root_path=args.workspace_root,
            reason="connect generated sanitized review workspace",
        )
        warnings.extend(sanitized.warnings)
        if sanitized.errors:
            errors.extend(scan.errors)
            errors.extend(sanitized.errors)
    elif needs_sanitized and dry_run:
        warnings.append("dry run: sanitized workspace would be generated before writing config")
    elif not scan.ok:
        errors.extend(scan.errors)

    bridge = agent_mode.check_bridge_executable(
        args.bridge_executable,
        project_dir=project,
        allow_project_bridge=args.allow_project_bridge,
        case_insensitive=case_insensitive,
    )
    warnings.extend(bridge.warnings)
    if not bridge.ok:
        errors.extend(bridge.errors)

    existing_roots = agent_mode.config_allowed_roots(config_path)
    additions = [str(allowed_root)]
    if sanitized and sanitized.workspace_dir:
        additions.append(
            str(
                agent_mode.resolve_path(
                    sanitized.workspace_allowed_root or sanitized.workspace_dir
                )
            )
        )
    merged_roots = agent_mode.merge_roots(existing_roots, additions, case_insensitive=case_insensitive)
    ok = not errors
    if ok and not dry_run and not args.no_write_config:
        agent_mode.write_agent_config_roots(merged_roots, path=config_path)

    return {
        "ok": ok,
        "project_dir": str(project),
        "allowed_root": str(allowed_root),
        "config_path": str(config_path),
        "wrote_config": ok and not dry_run and not args.no_write_config,
        "allowed_roots": merged_roots,
        "root_validation": root_result.__dict__,
        "secret_scan": scan.to_dict(),
        "sanitized_workspace": sanitized.to_dict() if sanitized else None,
        "bridge": bridge.__dict__,
        "errors": errors,
        "warnings": warnings,
        "dry_run": dry_run,
    }


def evaluate_status(args: argparse.Namespace) -> agent_mode.AgentModeStatus:
    roots = []
    if args.allowed_root:
        roots.append(str(agent_mode.resolve_path(args.allowed_root)))
    return agent_mode.evaluate_agent_mode(
        resolve_project(args.project_dir),
        mode="on",
        allowed_roots=roots or None,
        bridge_executable=args.bridge_executable,
        require_bridge=True,
        allow_project_bridge=args.allow_project_bridge,
        allow_sensitive_project=args.allow_sensitive_project,
        secret_scan=not args.no_secret_scan,
        sanitized_workspace_mode=args.sanitized_workspace,
        workspace_root_path=args.workspace_root,
        config=args.config_path,
        case_insensitive=args.case_insensitive_paths or None,
    )


def status_to_handoff(status: agent_mode.AgentModeStatus, task: str) -> str:
    return agent_mode.handoff_prompt(status, task=task, worktree=True)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def process_alive(pid: int, expected_identity: str = "") -> bool:
    return concurrency.process_alive(pid, expected_identity)


def terminate_process(pid: int, *, expected_identity: str = "", timeout: int = 8) -> bool:
    if not process_alive(pid, expected_identity):
        return False
    try:
        if os.name == "posix" and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid, expected_identity):
            return True
        time.sleep(0.2)
    try:
        if os.name == "posix" and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return not process_alive(pid, expected_identity)


def command_for_devspace(args: argparse.Namespace, status: agent_mode.AgentModeStatus) -> list[str]:
    bridge_path = status.bridge.resolved_path if status.bridge and status.bridge.resolved_path else args.bridge_executable
    return [bridge_path, "serve"]


def wait_for_url(log_path: Path, proc: subprocess.Popen[Any], timeout: int) -> tuple[str, str]:
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        if log_path.exists():
            try:
                last_text = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            except OSError:
                last_text = ""
            found = find_public_url_in_text(safety.redact_sensitive_text(last_text))
            if found:
                return found
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    return "", ""


def wait_for_quick_tunnel_url(log_path: Path, proc: subprocess.Popen[Any], timeout: int) -> tuple[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            except OSError:
                text = ""
            matches = list(QUICK_TUNNEL_URL_RE.finditer(text))
            if matches:
                return normalize_public_url(matches[-1].group(0))
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    return "", ""


def start_logged_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    label: str,
) -> subprocess.Popen[Any]:
    with log_path.open("ab", buffering=0) as log_handle:
        header = f"\n--- advisor_agent_connect {label} {utc_now()} cwd={cwd} ---\n"
        log_handle.write(header.encode("utf-8", errors="replace"))
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def start_quick_tunnel(
    args: argparse.Namespace,
    *,
    local_origin: str,
    cwd: Path,
    log_path: Path,
    on_started: Callable[[subprocess.Popen[Any]], None] | None = None,
) -> tuple[subprocess.Popen[Any], str, str]:
    executable = shutil.which(args.cloudflared_executable)
    if not executable:
        raise RuntimeError(
            "No cloudflared executable was found for automatic tunnel mode. "
            "Install cloudflared, pass --cloudflared-executable, or provide --public-base-url."
        )
    command = [executable, "tunnel", "--no-autoupdate", "--url", local_origin]
    safety.atomic_write_text(log_path, "")
    proc = start_logged_process(command, cwd=cwd, env=os.environ.copy(), log_path=log_path, label="cloudflared start")
    try:
        if on_started is not None:
            on_started(proc)
        base_url, mcp_url = wait_for_quick_tunnel_url(log_path, proc, args.timeout)
        if not mcp_url:
            raise RuntimeError("cloudflared started but did not publish a public HTTPS URL before timeout")
    except BaseException:
        terminate_process(proc.pid, expected_identity=concurrency.process_identity(proc.pid))
        raise
    return proc, base_url, mcp_url


def exposure_root_for_status(status: agent_mode.AgentModeStatus) -> Path:
    sanitized = status.sanitized_workspace
    if sanitized and sanitized.used and sanitized.workspace_allowed_root:
        return Path(sanitized.workspace_allowed_root).resolve()
    return Path(status.selected_root or status.project_dir).resolve()


def verify_devspace_readonly_mode(executable: str) -> None:
    patcher = Path(__file__).resolve().with_name("devspace_readonly_patch.py")
    completed = subprocess.run(
        [sys.executable, str(patcher), "--check", "--executable", executable],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=15,
    )
    if completed.returncode != 0:
        details = safety.redact_sensitive_text(completed.stderr.strip() or completed.stdout.strip())
        raise RuntimeError(
            "DevSpace does not have the required read-only advisor tool mode. "
            "Rerun this repository's setup script before serving the connector."
            + (f" Diagnostic: {details}" if details else "")
        )


def start_devspace_process(
    args: argparse.Namespace,
    *,
    status: agent_mode.AgentModeStatus,
    base_url: str,
    runtime: dict[str, Any],
    log_path: Path,
    exact_root_path: Path,
) -> subprocess.Popen[Any]:
    command = command_for_devspace(args, status)
    if not args.allow_unpatched_devspace:
        verify_devspace_readonly_mode(args.bridge_executable)
    env = os.environ.copy()
    env["DEVSPACE_PUBLIC_BASE_URL"] = base_url
    env["DEVSPACE_ALLOWED_ROOTS"] = str(exposure_root_for_status(status))
    env["HOST"] = str(runtime["host"])
    env["PORT"] = str(runtime["port"])
    env["DEVSPACE_TOOL_MODE"] = "readonly"
    env["DEVSPACE_READONLY_EXACT_ROOT_FILE"] = str(exact_root_path)
    env["DEVSPACE_SKILLS"] = "false"
    env["DEVSPACE_SKILL_PATHS"] = ""
    env["DEVSPACE_SUBAGENTS"] = "false"
    env["DEVSPACE_WIDGETS"] = "off"
    env["DEVSPACE_LOG_REQUESTS"] = "false"
    env["DEVSPACE_LOG_ASSETS"] = "false"
    env["DEVSPACE_LOG_TOOL_CALLS"] = "true"
    env["DEVSPACE_LOG_SHELL_COMMANDS"] = "false"
    env["DEVSPACE_TRUST_PROXY"] = "false"
    env["DEVSPACE_OAUTH_ALLOWED_REDIRECT_HOSTS"] = "chatgpt.com,localhost,127.0.0.1"
    return start_logged_process(
        command,
        cwd=Path(status.project_dir),
        env=env,
        log_path=log_path,
        label="devspace start",
    )


def stop_recorded_processes(state: dict[str, Any]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for name in ("devspace", "tunnel"):
        pid_key = f"{name}_pid"
        identity_key = f"{name}_process_identity"
        pid = int(state.get(pid_key) or (state.get("pid") if name == "devspace" else 0) or 0)
        identity = str(state.get(identity_key) or "")
        results[name] = terminate_process(pid, expected_identity=identity) if pid else False
    return results


def connector_runtime_status(
    project: Path,
    *,
    root: Path,
    probe_timeout: float = 4.0,
    skip_public_probe: bool = False,
) -> dict[str, Any]:
    paths = state_paths(project, root)
    state = read_state(paths["state"])
    if not state:
        return {
            "lifecycle_state": "absent",
            "connector_ready": False,
            "state_path": str(paths["state"]),
        }
    devspace_pid = int(state.get("devspace_pid") or state.get("pid") or 0)
    tunnel_pid = int(state.get("tunnel_pid") or 0)
    devspace_running = process_alive(devspace_pid, str(state.get("devspace_process_identity") or ""))
    tunnel_managed = bool(tunnel_pid)
    tunnel_running = (
        process_alive(tunnel_pid, str(state.get("tunnel_process_identity") or ""))
        if tunnel_managed
        else False
    )
    tunnel_available = tunnel_running if tunnel_managed else True
    workspace = Path(str(state.get("agent_workspace") or ""))
    workspace_exists = workspace.is_dir() if str(workspace) not in {"", "."} else False
    exact_root_path = Path(str(state.get("readonly_exact_root_file") or paths["exact_root"])).expanduser().resolve()
    expected_exact_root_path = paths["exact_root"].resolve()
    exact_root_ready = bool(
        workspace_exists
        and exact_root_path == expected_exact_root_path
        and exact_root_matches(exact_root_path, workspace)
    )
    local_url = str(state.get("local_mcp_url") or "")
    public_url = str(state.get("mcp_url") or "")
    readonly_tool_mode = state.get("tool_mode") == "readonly"
    chatgpt_attachment_verified = bool(state.get("chatgpt_attachment_verified"))
    local_probe = probe_mcp_url(local_url, timeout=probe_timeout) if devspace_running and local_url else {}
    if skip_public_probe:
        public_probe = {
            "ready": True,
            "status": 0,
            "oauth_challenge": False,
            "checked_utc": utc_now(),
            "error": "",
            "skipped": True,
        }
    else:
        public_probe = probe_mcp_url(public_url, timeout=probe_timeout) if devspace_running and public_url else {}
    ready = bool(
        devspace_running
        and tunnel_available
        and workspace_exists
        and exact_root_ready
        and readonly_tool_mode
        and local_probe.get("ready")
        and public_probe.get("ready")
    )
    recorded_lifecycle = str(state.get("lifecycle_state") or "")
    if ready:
        lifecycle_state = "connector-ready"
    elif recorded_lifecycle == "starting" and (devspace_running or (tunnel_managed and tunnel_running)):
        lifecycle_state = "starting"
    elif recorded_lifecycle in {"failed", "stopped"}:
        lifecycle_state = recorded_lifecycle
    else:
        lifecycle_state = "stale"
    result = {
        **state,
        "state_path": str(paths["state"]),
        "devspace_running": devspace_running,
        "tunnel_managed": tunnel_managed,
        "tunnel_running": tunnel_running,
        "tunnel_available": tunnel_available,
        "workspace_exists": workspace_exists,
        "readonly_exact_root_ready": exact_root_ready,
        "readonly_tool_mode": readonly_tool_mode,
        "chatgpt_attachment_verified": chatgpt_attachment_verified,
        "local_probe": local_probe,
        "public_probe": public_probe,
        "connector_ready": ready,
        "agent_mode_ready": ready and chatgpt_attachment_verified,
        "lifecycle_state": lifecycle_state,
        "checked_utc": utc_now(),
    }
    return result


def pin_connector_workspace(
    project: Path,
    expected_state: dict[str, Any],
    workspace: Path,
    *,
    generation: str = "",
    fingerprint: str = "",
) -> dict[str, Any]:
    """Atomically move a live read-only connector to one verified snapshot."""
    root = runtime_root()
    paths = state_paths(project, root)
    stable_fields = (
        "started_utc",
        "devspace_pid",
        "devspace_process_identity",
        "tunnel_pid",
        "tunnel_process_identity",
        "mcp_url",
        "allowed_root",
        "readonly_exact_root_file",
        "tool_mode",
    )
    workspace = workspace.expanduser().resolve()
    with concurrency.InterProcessLock(paths["lock"], timeout=30.0):
        current = read_state(paths["state"])
        if not current or any(current.get(key) != expected_state.get(key) for key in stable_fields):
            raise RuntimeError("The DevSpace connector changed before its review snapshot could be pinned.")
        allowed_root = Path(str(current.get("allowed_root") or "")).expanduser().resolve()
        if not agent_mode.path_is_same_or_child(workspace, allowed_root):
            raise RuntimeError("The refreshed review snapshot is outside the connector's generated workspace root.")
        write_exact_root(paths["exact_root"], workspace)
        current.update(
            {
                "agent_workspace": str(workspace),
                "readonly_exact_root_file": str(paths["exact_root"]),
                "workspace_generation": generation,
                "workspace_fingerprint": fingerprint,
                "updated_utc": utc_now(),
            }
        )
        safety.atomic_write_json(paths["state"], current)
    return connector_runtime_status(project, root=root, skip_public_probe=True)


def print_connect_summary(
    *,
    setup: dict[str, Any],
    status: agent_mode.AgentModeStatus,
    mcp_url: str,
    public_source: str,
    handoff: str,
    state: dict[str, Any] | None = None,
) -> None:
    print("Advisor Agent Connect")
    print(f"project_dir: {setup['project_dir']}")
    print(f"agent_workspace: {status.project_dir}")
    print(f"workspace_mode: {'sanitized_copy' if status.sanitized_workspace and status.sanitized_workspace.used else 'project_or_worktree'}")
    print(f"wrote_config: {'yes' if setup['wrote_config'] else 'no'}")
    if status.sanitized_workspace and status.sanitized_workspace.used:
        print(f"sanitized_workspace_dir: {status.sanitized_workspace.workspace_dir}")
    if mcp_url:
        print(f"chatgpt_connector_url: {mcp_url}")
        print(f"url_source: {public_source or 'devspace output'}")
    else:
        print("chatgpt_connector_url: unavailable")
        print("url_hint: pass --public-base-url https://your-tunnel.example.com or set DEVSPACE_PUBLIC_BASE_URL")
    if state:
        print(f"lifecycle_state: {state.get('lifecycle_state', 'unknown')}")
        print(f"connector_ready: {'yes' if state.get('connector_ready') else 'no'}")
        print(
            "chatgpt_attachment_verified: "
            f"{'yes' if state.get('chatgpt_attachment_verified') else 'no'}"
        )
        print(f"agent_mode_ready: {'yes' if state.get('agent_mode_ready') else 'no'}")
        print(f"devspace_pid: {state.get('devspace_pid', state.get('pid', ''))}")
        if state.get("tunnel_pid"):
            print(f"tunnel_pid: {state.get('tunnel_pid')}")
        print(f"devspace_log: {state.get('log_path', '')}")
    print("chatgpt_steps:")
    print("- ChatGPT Settings -> Apps & Connectors -> Advanced settings -> enable Developer Mode.")
    print("- Create app/connector and paste the chatgpt_connector_url above.")
    print("- Open a new chat, choose Developer Mode, enable that connector, then paste the handoff below.")
    print("")
    print(handoff.rstrip())


def command_prepare(args: argparse.Namespace) -> int:
    setup = configure_allowed_roots(args, dry_run=args.dry_run)
    if not setup["ok"]:
        print("Advisor agent prepare failed:", file=sys.stderr)
        for item in setup["errors"]:
            print(f"- {item}", file=sys.stderr)
        return 2
    status = evaluate_status(args)
    if not status.available:
        print(agent_mode.render_status_text(status, include_node=False), file=sys.stderr)
        return 2
    bridge_path = status.bridge.resolved_path if status.bridge and status.bridge.resolved_path else args.bridge_executable
    base_url, mcp_url, source = discover_public_url(args, bridge_path, Path(status.project_dir))
    handoff = status_to_handoff(status, args.task or "")
    print_connect_summary(setup=setup, status=status, mcp_url=mcp_url, public_source=source, handoff=handoff)
    if args.open_chatgpt_settings:
        webbrowser.open("https://chatgpt.com/#settings/Apps")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    paths = state_paths(resolve_project(args.project_dir), runtime_root(args.runtime_root))
    lifecycle_lock = concurrency.InterProcessLock(
        paths["lock"],
        timeout=max(60.0, float(args.timeout) + 30.0),
        wait_message="Advisor agent connector setup is queued behind another session for this project.",
    )
    with lifecycle_lock:
        setup = configure_allowed_roots(args, dry_run=False)
        if not setup["ok"]:
            print("Advisor agent serve failed during setup:", file=sys.stderr)
            for item in setup["errors"]:
                print(f"- {item}", file=sys.stderr)
            return 2
        status = evaluate_status(args)
        if not status.available:
            print(agent_mode.render_status_text(status, include_node=False), file=sys.stderr)
            return 2

        existing = connector_runtime_status(
            resolve_project(args.project_dir),
            root=runtime_root(args.runtime_root),
            skip_public_probe=args.skip_public_probe,
        )
        existing_root = Path(str(existing.get("allowed_root") or "")) if existing.get("allowed_root") else None
        latest_workspace = Path(status.project_dir).resolve()
        existing_covers_workspace = bool(
            existing_root
            and existing_root.exists()
            and agent_mode.path_is_same_or_child(latest_workspace, existing_root)
        )
        if (
            existing.get("connector_ready")
            and existing.get("readonly_tool_mode")
            and existing_covers_workspace
            and not args.force
        ):
            write_exact_root(paths["exact_root"], latest_workspace)
            existing.update(
                {
                    "agent_workspace": str(latest_workspace),
                    "readonly_exact_root_file": str(paths["exact_root"]),
                    "workspace_generation": (
                        status.sanitized_workspace.generation_id
                        if status.sanitized_workspace and status.sanitized_workspace.used
                        else ""
                    ),
                    "workspace_fingerprint": (
                        status.sanitized_workspace.source_fingerprint
                        if status.sanitized_workspace and status.sanitized_workspace.used
                        else ""
                    ),
                    "updated_utc": utc_now(),
                }
            )
            safety.atomic_write_json(paths["state"], existing)
            existing = connector_runtime_status(
                resolve_project(args.project_dir),
                root=runtime_root(args.runtime_root),
                skip_public_probe=args.skip_public_probe,
            )
            handoff = status_to_handoff(status, args.task or "")
            print_connect_summary(
                setup=setup,
                status=status,
                mcp_url=str(existing.get("mcp_url") or ""),
                public_source=str(existing.get("url_source") or ""),
                handoff=handoff,
                state=existing,
            )
            print("note: verified connector reused; the handoff is pinned to the current sanitized workspace generation.")
            return 0
        if existing.get("devspace_running") or (
            existing.get("tunnel_managed") and existing.get("tunnel_running")
        ):
            stop_recorded_processes(existing)

        bridge_path = status.bridge.resolved_path if status.bridge and status.bridge.resolved_path else args.bridge_executable
        runtime = read_devspace_runtime(bridge_path, Path(status.project_dir))
        base_url, mcp_url, source = discover_public_url(args, bridge_path, Path(status.project_dir))
        if source == "devspace config publicBaseUrl" and args.tunnel_mode == "auto":
            configured_probe = probe_mcp_url(mcp_url, timeout=2.0)
            if configured_probe.get("status") == 0:
                base_url, mcp_url, source = "", "", ""
        tunnel_proc: subprocess.Popen[Any] | None = None
        devspace_proc: subprocess.Popen[Any] | None = None
        attempt_started_utc = utc_now()
        write_exact_root(paths["exact_root"], Path(status.project_dir))

        def persist_lifecycle(
            lifecycle_state: str,
            *,
            current_devspace: subprocess.Popen[Any] | None,
            current_tunnel: subprocess.Popen[Any] | None,
            selected_base_url: str,
            selected_mcp_url: str,
            selected_source: str,
            connector_ready: bool = False,
            local_probe: dict[str, Any] | None = None,
            public_probe: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            allowed_root = exposure_root_for_status(status)
            state = {
                "schema_version": "2.0",
                "lifecycle_state": lifecycle_state,
                "connector_ready": connector_ready,
                "started_utc": attempt_started_utc,
                "updated_utc": utc_now(),
                "project_dir": setup["project_dir"],
                "agent_workspace": status.project_dir,
                "allowed_root": str(allowed_root),
                "workspace_generation": (
                    status.sanitized_workspace.generation_id
                    if status.sanitized_workspace and status.sanitized_workspace.used
                    else ""
                ),
                "workspace_fingerprint": (
                    status.sanitized_workspace.source_fingerprint
                    if status.sanitized_workspace and status.sanitized_workspace.used
                    else ""
                ),
                "readonly_exact_root_file": str(paths["exact_root"]),
                "tool_mode": "readonly",
                "chatgpt_attachment_verified": False,
                "agent_mode_ready": False,
                "local_mcp_url": str(runtime["mcp_url"]),
                "mcp_url": selected_mcp_url,
                "public_base_url": selected_base_url,
                "url_source": selected_source,
                "devspace_pid": current_devspace.pid if current_devspace else 0,
                "devspace_process_identity": (
                    concurrency.process_identity(current_devspace.pid) if current_devspace else ""
                ),
                "tunnel_pid": current_tunnel.pid if current_tunnel else 0,
                "tunnel_process_identity": (
                    concurrency.process_identity(current_tunnel.pid) if current_tunnel else ""
                ),
                "log_path": str(paths["log"]),
                "tunnel_log_path": str(paths["tunnel_log"]) if current_tunnel else "",
                "command": safety.redact_argv(command_for_devspace(args, status)),
            }
            if local_probe is not None:
                state["local_probe"] = local_probe
            if public_probe is not None:
                state["public_probe"] = public_probe
            if connector_ready:
                state["verified_utc"] = utc_now()
            elif lifecycle_state == "failed":
                state["failed_utc"] = utc_now()
            safety.atomic_write_json(paths["state"], state)
            return state

        if args.foreground:
            if not base_url:
                raise RuntimeError("foreground mode requires --public-base-url or a configured DevSpace publicBaseUrl")
            command = command_for_devspace(args, status)
            if not args.allow_unpatched_devspace:
                verify_devspace_readonly_mode(args.bridge_executable)
            env = os.environ.copy()
            env["DEVSPACE_PUBLIC_BASE_URL"] = base_url
            env["DEVSPACE_ALLOWED_ROOTS"] = str(exposure_root_for_status(status))
            env["HOST"] = str(runtime["host"])
            env["PORT"] = str(runtime["port"])
            env["DEVSPACE_TOOL_MODE"] = "readonly"
            env["DEVSPACE_READONLY_EXACT_ROOT_FILE"] = str(paths["exact_root"])
            env["DEVSPACE_SKILLS"] = "false"
            env["DEVSPACE_SKILL_PATHS"] = ""
            env["DEVSPACE_SUBAGENTS"] = "false"
            env["DEVSPACE_WIDGETS"] = "off"
            env["DEVSPACE_LOG_REQUESTS"] = "false"
            env["DEVSPACE_LOG_ASSETS"] = "false"
            env["DEVSPACE_LOG_TOOL_CALLS"] = "true"
            env["DEVSPACE_LOG_SHELL_COMMANDS"] = "false"
            env["DEVSPACE_TRUST_PROXY"] = "false"
            env["DEVSPACE_OAUTH_ALLOWED_REDIRECT_HOSTS"] = "chatgpt.com,localhost,127.0.0.1"
            print(f"chatgpt_connector_url: {mcp_url}")
            print("Starting DevSpace in foreground. Press Ctrl-C to stop it.", file=sys.stderr)
            os.execvpe(command[0], command, env)

        def launch_with_url(selected_base: str, selected_mcp: str, selected_source: str) -> tuple[subprocess.Popen[Any], dict[str, Any], dict[str, Any]]:
            proc = start_devspace_process(
                args,
                status=status,
                base_url=selected_base,
                runtime=runtime,
                log_path=paths["log"],
                exact_root_path=paths["exact_root"],
            )
            try:
                persist_lifecycle(
                    "starting",
                    current_devspace=proc,
                    current_tunnel=tunnel_proc,
                    selected_base_url=selected_base,
                    selected_mcp_url=selected_mcp,
                    selected_source=selected_source,
                )
                local_probe, public_probe = wait_for_mcp_readiness(
                    local_url=str(runtime["mcp_url"]),
                    public_url=selected_mcp,
                    processes=[item for item in (proc, tunnel_proc) if item is not None],
                    timeout=args.timeout,
                    skip_public_probe=args.skip_public_probe,
                )
            except BaseException:
                terminate_process(proc.pid, expected_identity=concurrency.process_identity(proc.pid))
                raise
            return proc, local_probe, public_probe

        try:
            persist_lifecycle(
                "starting",
                current_devspace=None,
                current_tunnel=None,
                selected_base_url=base_url,
                selected_mcp_url=mcp_url,
                selected_source=source,
            )
            if not base_url:
                if args.tunnel_mode != "auto":
                    raise RuntimeError(
                        "No public HTTPS URL is configured. Use --tunnel-mode auto, "
                        "pass --public-base-url, or set DEVSPACE_PUBLIC_BASE_URL."
                    )
                tunnel_proc, base_url, mcp_url = start_quick_tunnel(
                    args,
                    local_origin=str(runtime["origin"]),
                    cwd=Path(status.project_dir),
                    log_path=paths["tunnel_log"],
                    on_started=lambda proc: persist_lifecycle(
                        "starting",
                        current_devspace=devspace_proc,
                        current_tunnel=proc,
                        selected_base_url="",
                        selected_mcp_url="",
                        selected_source="managed cloudflared quick tunnel (starting)",
                    ),
                )
                source = "managed cloudflared quick tunnel"
                persist_lifecycle(
                    "starting",
                    current_devspace=devspace_proc,
                    current_tunnel=tunnel_proc,
                    selected_base_url=base_url,
                    selected_mcp_url=mcp_url,
                    selected_source=source,
                )

            devspace_proc, local_probe, public_probe = launch_with_url(base_url, mcp_url, source)
            if not (local_probe.get("ready") and public_probe.get("ready")):
                can_replace_stale_config = (
                    args.tunnel_mode == "auto"
                    and source == "devspace config publicBaseUrl"
                    and tunnel_proc is None
                )
                if not can_replace_stale_config:
                    raise RuntimeError(
                        "DevSpace did not become connector-ready: "
                        f"local={local_probe.get('error') or local_probe.get('status')}; "
                        f"public={public_probe.get('error') or public_probe.get('status')}"
                    )
                terminate_process(
                    devspace_proc.pid,
                    expected_identity=concurrency.process_identity(devspace_proc.pid),
                )
                devspace_proc = None
                tunnel_proc, base_url, mcp_url = start_quick_tunnel(
                    args,
                    local_origin=str(runtime["origin"]),
                    cwd=Path(status.project_dir),
                    log_path=paths["tunnel_log"],
                    on_started=lambda proc: persist_lifecycle(
                        "starting",
                        current_devspace=devspace_proc,
                        current_tunnel=proc,
                        selected_base_url="",
                        selected_mcp_url="",
                        selected_source="managed cloudflared quick tunnel (starting)",
                    ),
                )
                source = "managed cloudflared quick tunnel replacing stale DevSpace config"
                persist_lifecycle(
                    "starting",
                    current_devspace=devspace_proc,
                    current_tunnel=tunnel_proc,
                    selected_base_url=base_url,
                    selected_mcp_url=mcp_url,
                    selected_source=source,
                )
                devspace_proc, local_probe, public_probe = launch_with_url(base_url, mcp_url, source)
                if not (local_probe.get("ready") and public_probe.get("ready")):
                    raise RuntimeError(
                        "Managed quick tunnel did not become connector-ready: "
                        f"local={local_probe.get('error') or local_probe.get('status')}; "
                        f"public={public_probe.get('error') or public_probe.get('status')}"
                    )

            state = persist_lifecycle(
                "connector-ready",
                current_devspace=devspace_proc,
                current_tunnel=tunnel_proc,
                selected_base_url=base_url,
                selected_mcp_url=mcp_url,
                selected_source=source,
                connector_ready=True,
                local_probe=local_probe,
                public_probe=public_probe,
            )
            handoff = status_to_handoff(status, args.task or "")
            print_connect_summary(
                setup=setup,
                status=status,
                mcp_url=mcp_url,
                public_source=source,
                handoff=handoff,
                state=state,
            )
            if args.open_chatgpt_settings:
                webbrowser.open("https://chatgpt.com/#settings/Apps")
            return 0
        except BaseException:
            if devspace_proc is not None:
                terminate_process(
                    devspace_proc.pid,
                    expected_identity=concurrency.process_identity(devspace_proc.pid),
                )
            if tunnel_proc is not None:
                terminate_process(
                    tunnel_proc.pid,
                    expected_identity=concurrency.process_identity(tunnel_proc.pid),
                )
            persist_lifecycle(
                "failed",
                current_devspace=None,
                current_tunnel=None,
                selected_base_url=base_url,
                selected_mcp_url=mcp_url,
                selected_source=source,
            )
            raise


def command_stop(args: argparse.Namespace) -> int:
    project = resolve_project(args.project_dir)
    root = runtime_root(args.runtime_root)
    paths = state_paths(project, root)
    with concurrency.InterProcessLock(paths["lock"], timeout=30.0):
        state = read_state(paths["state"])
        if not state:
            print("No advisor connector process state recorded for this project.")
            return 0
        before = connector_runtime_status(
            project,
            root=root,
            skip_public_probe=True,
        )
        stopped = stop_recorded_processes(state)
        state.update(
            {
                "lifecycle_state": "stopped",
                "connector_ready": False,
                "stopped_utc": utc_now(),
                "devspace_pid": 0,
                "tunnel_pid": 0,
            }
        )
        safety.atomic_write_json(paths["state"], state)
    print(f"devspace_stopped: {'yes' if stopped['devspace'] or not before.get('devspace_running') else 'no'}")
    print(f"tunnel_stopped: {'yes' if stopped['tunnel'] or not before.get('tunnel_managed') or not before.get('tunnel_running') else 'no'}")
    print(f"state_path: {paths['state']}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    state = connector_runtime_status(
        resolve_project(args.project_dir),
        root=runtime_root(args.runtime_root),
        skip_public_probe=args.skip_public_probe,
    )
    print(json.dumps(state, indent=2))
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project directory to expose or sanitize.")
    parser.add_argument("--allowed-root", type=Path, help="Allowed root to store. Defaults to the exact project directory.")
    parser.add_argument("--config-path", help="User-level config path. Defaults to ~/.codex/advisor-agent/config.json.")
    parser.add_argument("--bridge-executable", default=os.environ.get(agent_mode.BRIDGE_EXECUTABLE_ENV, agent_mode.DEFAULT_BRIDGE_EXECUTABLE))
    parser.add_argument(
        "--sanitized-workspace",
        choices=sorted(agent_mode.VALID_SANITIZED_WORKSPACE_MODES),
        default=os.environ.get(
            agent_mode.SANITIZED_WORKSPACE_ENV,
            agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE,
        ),
    )
    parser.add_argument("--workspace-root", help="Root for generated sanitized workspaces. Defaults to ~/.codex/advisor-agent/workspaces.")
    parser.add_argument("--runtime-root", help="Root for DevSpace pid/log state. Defaults to ~/.codex/advisor-agent/devspace.")
    parser.add_argument("--public-base-url", help="Public HTTPS tunnel base URL. The script prints this with /mcp for ChatGPT.")
    parser.add_argument("--task", help="Task text to include in the generated ChatGPT handoff.")
    parser.add_argument("--open-chatgpt-settings", action="store_true", help="Open ChatGPT settings page in the default browser after printing the URL.")
    parser.add_argument("--allow-project-bridge", action="store_true", help="Allow bridge executable paths inside the project. Diagnostic only.")
    parser.add_argument("--allow-sensitive-project", action="store_true", help="Allow agent-mode despite sensitive findings. Diagnostic only.")
    parser.add_argument("--no-secret-scan", action="store_true", help="Disable project secret preflight. Diagnostic only.")
    parser.add_argument("--case-insensitive-paths", action="store_true", help="Use case-insensitive path containment checks.")
    parser.add_argument("--no-write-config", action="store_true", help="Do not write/update the user-level allowed-root config.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Prepare config/workspace and print ChatGPT connector URL/handoff; do not start DevSpace.")
    add_common_args(prepare)
    prepare.add_argument("--dry-run", action="store_true", help="Validate without writing config or generating sanitized workspaces.")
    prepare.set_defaults(func=command_prepare)

    serve = sub.add_parser("serve", help="Prepare config/workspace, start DevSpace, and print the ChatGPT connector URL/handoff.")
    add_common_args(serve)
    serve.add_argument("--timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT, help="Seconds to wait for the tunnel and MCP readiness checks.")
    serve.add_argument(
        "--tunnel-mode",
        choices=sorted(VALID_TUNNEL_MODES),
        default="auto",
        help="Use a managed cloudflared quick tunnel automatically, require configured URL state, or disable tunnel startup.",
    )
    serve.add_argument("--cloudflared-executable", default="cloudflared", help="cloudflared executable used by automatic tunnel mode.")
    serve.add_argument("--skip-public-probe", action="store_true", help="Skip the public MCP health probe. Diagnostic/testing only.")
    serve.add_argument(
        "--allow-unpatched-devspace",
        action="store_true",
        help="Diagnostic/testing only: start without verifying the mechanically read-only DevSpace patch.",
    )
    serve.add_argument("--foreground", action="store_true", help="Run DevSpace in the foreground after printing any known URL.")
    serve.add_argument("--force", action="store_true", help="Restart an existing recorded DevSpace process for this project.")
    serve.set_defaults(func=command_serve)

    stop = sub.add_parser("stop", help="Stop a background DevSpace process started for this project.")
    stop.add_argument("--project-dir", type=Path, default=Path.cwd())
    stop.add_argument("--runtime-root", help="Root for DevSpace pid/log state. Defaults to ~/.codex/advisor-agent/devspace.")
    stop.set_defaults(func=command_stop)

    status = sub.add_parser("status", help="Show recorded background DevSpace state for this project.")
    status.add_argument("--project-dir", type=Path, default=Path.cwd())
    status.add_argument("--runtime-root", help="Root for DevSpace pid/log state. Defaults to ~/.codex/advisor-agent/devspace.")
    status.add_argument("--skip-public-probe", action="store_true", help="Skip the public MCP health probe. Diagnostic/testing only.")
    status.set_defaults(func=command_status)
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Advisor agent connect failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
