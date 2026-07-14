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
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import advisor_safety as safety
import agent_mode


PUBLIC_BASE_URL_ENV = "ADVISOR_AGENT_PUBLIC_BASE_URL"
RUNTIME_ROOT_ENV = "ADVISOR_AGENT_RUNTIME_ROOT"
DEFAULT_CONNECT_TIMEOUT = 30
HTTPS_URL_RE = re.compile(r"https://[^\s\"'<>]+")


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
    }


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
        additions.append(str(agent_mode.resolve_path(sanitized.workspace_dir)))
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


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process(pid: int, timeout: int = 8) -> bool:
    if not process_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return not process_alive(pid)


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
        print(f"devspace_pid: {state.get('pid', '')}")
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

    bridge_path = status.bridge.resolved_path if status.bridge and status.bridge.resolved_path else args.bridge_executable
    base_url, mcp_url, source = discover_public_url(args, bridge_path, Path(status.project_dir))
    paths = state_paths(resolve_project(args.project_dir), runtime_root(args.runtime_root))
    existing = read_state(paths["state"])
    pid = int(existing.get("pid") or 0)
    if pid and process_alive(pid) and not args.force:
        handoff = status_to_handoff(status, args.task or "")
        print_connect_summary(setup=setup, status=status, mcp_url=existing.get("mcp_url", mcp_url), public_source=existing.get("url_source", source), handoff=handoff, state=existing)
        print("note: existing DevSpace process is still running; use --force to restart or stop first.")
        return 0
    if pid and process_alive(pid):
        terminate_process(pid)

    command = command_for_devspace(args, status)
    env = os.environ.copy()
    if base_url:
        env["DEVSPACE_PUBLIC_BASE_URL"] = base_url
    if args.foreground:
        if mcp_url:
            print(f"chatgpt_connector_url: {mcp_url}")
            print("Paste that URL into ChatGPT Settings -> Apps & Connectors -> Create app.")
        print("Starting DevSpace in foreground. Press Ctrl-C to stop it.", file=sys.stderr)
        os.execvpe(command[0], command, env)

    with paths["log"].open("ab", buffering=0) as log_handle:
        header = f"\n--- advisor_agent_connect start {utc_now()} cwd={status.project_dir} ---\n"
        log_handle.write(header.encode("utf-8", errors="replace"))
        proc = subprocess.Popen(
            command,
            cwd=status.project_dir,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    if not mcp_url:
        base_url, mcp_url = wait_for_url(paths["log"], proc, args.timeout)
        source = "devspace output" if mcp_url else source
    if not mcp_url:
        terminate_process(proc.pid)
        print("DevSpace started but no public HTTPS URL was found before timeout.", file=sys.stderr)
        print("Set DEVSPACE_PUBLIC_BASE_URL or rerun with --public-base-url https://your-tunnel.example.com", file=sys.stderr)
        print(f"devspace_log: {paths['log']}", file=sys.stderr)
        return 2

    state = {
        "schema_version": "1.0",
        "started_utc": utc_now(),
        "pid": proc.pid,
        "project_dir": setup["project_dir"],
        "agent_workspace": status.project_dir,
        "mcp_url": mcp_url,
        "public_base_url": base_url,
        "url_source": source,
        "log_path": str(paths["log"]),
        "command": safety.redact_argv(command),
    }
    safety.atomic_write_json(paths["state"], state)
    handoff = status_to_handoff(status, args.task or "")
    print_connect_summary(setup=setup, status=status, mcp_url=mcp_url, public_source=source, handoff=handoff, state=state)
    if args.open_chatgpt_settings:
        webbrowser.open("https://chatgpt.com/#settings/Apps")
    return 0


def command_stop(args: argparse.Namespace) -> int:
    paths = state_paths(resolve_project(args.project_dir), runtime_root(args.runtime_root))
    state = read_state(paths["state"])
    pid = int(state.get("pid") or 0)
    if not pid:
        print("No DevSpace process state recorded for this project.")
        return 0
    if not process_alive(pid):
        print(f"Recorded DevSpace process is not running: pid={pid}")
        return 0
    stopped = terminate_process(pid)
    print(f"stopped: {'yes' if stopped else 'no'}")
    print(f"pid: {pid}")
    print(f"state_path: {paths['state']}")
    return 0 if stopped else 2


def command_status(args: argparse.Namespace) -> int:
    paths = state_paths(resolve_project(args.project_dir), runtime_root(args.runtime_root))
    state = read_state(paths["state"])
    if not state:
        print("No advisor DevSpace state recorded for this project.")
        return 0
    pid = int(state.get("pid") or 0)
    state["running"] = process_alive(pid)
    print(json.dumps(state, indent=2))
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project directory to expose or sanitize.")
    parser.add_argument("--allowed-root", type=Path, help="Allowed root to store. Defaults to the exact project directory.")
    parser.add_argument("--config-path", help="User-level config path. Defaults to ~/.codex/advisor-agent/config.json.")
    parser.add_argument("--bridge-executable", default=os.environ.get(agent_mode.BRIDGE_EXECUTABLE_ENV, agent_mode.DEFAULT_BRIDGE_EXECUTABLE))
    parser.add_argument("--sanitized-workspace", choices=sorted(agent_mode.VALID_SANITIZED_WORKSPACE_MODES), default=os.environ.get(agent_mode.SANITIZED_WORKSPACE_ENV, "auto"))
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
    serve.add_argument("--timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT, help="Seconds to wait for DevSpace output to reveal a public URL.")
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
