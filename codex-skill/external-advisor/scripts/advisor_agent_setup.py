#!/usr/bin/env python3
"""Configure repo-aware advisor agent-mode for one local project.

This helper writes a user-level allowed-root config after validating the target
project. It does not install DevSpace, run npx, start a tunnel, contact
ChatGPT, write credentials, or modify repository files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import agent_mode


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="Project directory to authorize.")
    parser.add_argument(
        "--allowed-root",
        type=Path,
        help="Allowed root to store. Defaults to the exact project directory, which is the preferred narrow setting.",
    )
    parser.add_argument("--config-path", help="User-level config path. Defaults to ~/.codex/advisor-agent/config.json.")
    parser.add_argument("--bridge-executable", default=os.environ.get(agent_mode.BRIDGE_EXECUTABLE_ENV, agent_mode.DEFAULT_BRIDGE_EXECUTABLE))
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the config change without writing it.")
    parser.add_argument("--auto", action="store_true", help="Accept the default exact-project allowed root when --allowed-root is omitted.")
    parser.add_argument("--allow-sensitive-project", action="store_true", help="Write config even when secret preflight finds sensitive files. Diagnostic only.")
    parser.add_argument(
        "--sanitized-workspace",
        choices=sorted(agent_mode.VALID_SANITIZED_WORKSPACE_MODES),
        default=os.environ.get(agent_mode.SANITIZED_WORKSPACE_ENV, agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE),
        help="Create a sanitized review copy automatically, always, or never.",
    )
    parser.add_argument("--workspace-root", help="Root for generated sanitized workspaces. Defaults to ~/.codex/advisor-agent/workspaces.")
    parser.add_argument("--allow-project-bridge", action="store_true", help="Allow a bridge executable inside the project. Diagnostic only.")
    parser.add_argument("--case-insensitive-paths", action="store_true", help="Use case-insensitive path containment checks.")
    parser.add_argument("--json", action="store_true", help="Print JSON status.")
    return parser.parse_args()


def render_text(payload: dict[str, Any]) -> str:
    lines = ["Advisor Agent Setup"]
    lines.append(f"project_dir: {payload['project_dir']}")
    lines.append(f"allowed_root: {payload['allowed_root']}")
    lines.append(f"config_path: {payload['config_path']}")
    lines.append(f"would_write: {'yes' if payload['would_write'] else 'no'}")
    lines.append(f"wrote_config: {'yes' if payload['wrote_config'] else 'no'}")
    lines.append(f"secret_scan_ok: {'yes' if payload['secret_scan']['ok'] else 'no'}")
    lines.append(f"secret_scan_files: {payload['secret_scan']['scanned_files']}")
    sanitized = payload["sanitized_workspace"]
    lines.append(f"sanitized_workspace_used: {'yes' if sanitized and sanitized['used'] else 'no'}")
    if sanitized and sanitized["workspace_dir"]:
        lines.append(f"sanitized_workspace_dir: {sanitized['workspace_dir']}")
        lines.append(f"sanitized_copied_files: {sanitized['copied_files']}")
        lines.append(f"sanitized_skipped_files: {sanitized['skipped_files']}")
        lines.append(f"sanitized_skipped_dirs: {sanitized['skipped_dirs']}")
        lines.append(f"sanitized_skipped_symlinks: {sanitized['skipped_symlinks']}")
    if payload["secret_scan"]["findings"]:
        lines.append("secret_findings:")
        for finding in payload["secret_scan"]["findings"][:12]:
            lines.append(f"- {finding['path']}: {finding['reason']}")
        remaining = len(payload["secret_scan"]["findings"]) - 12
        if remaining > 0:
            lines.append(f"- ... {remaining} more omitted")
    bridge = payload["bridge"]
    lines.append(f"bridge_executable: {bridge['executable']}")
    lines.append(f"bridge_path: {bridge['resolved_path'] or 'not found'}")
    lines.append(f"bridge_ok: {'yes' if bridge['ok'] else 'no'}")
    if payload["errors"]:
        lines.append("errors:")
        for item in payload["errors"]:
            lines.append(f"- {item}")
    if payload["warnings"]:
        lines.append("warnings:")
        for item in payload["warnings"]:
            lines.append(f"- {item}")
    lines.append("next_steps:")
    lines.append("- Install DevSpace if bridge_ok is no: npm install -g @waishnav/devspace")
    if sanitized and sanitized["workspace_dir"]:
        lines.append("- Run DevSpace setup so the generated sanitized workspace root can be opened when needed.")
    else:
        lines.append("- Run DevSpace setup yourself for this exact project root: devspace init")
    lines.append("- Start the bridge yourself when needed: devspace serve")
    lines.append("- Verify routing: python3 ~/.codex/skills/external-advisor/scripts/agent_mode.py --doctor --project-dir .")
    lines.append("dry_run_safety: no DevSpace process launched, no tunnel opened, no ChatGPT request made, no credentials written")
    return "\n".join(lines)


def main() -> int:
    configure_stdio()
    args = parse_args()
    case_insensitive = args.case_insensitive_paths or agent_mode.default_case_insensitive()
    project = agent_mode.resolve_path(args.project_dir)
    allowed_root = agent_mode.resolve_path(args.allowed_root) if args.allowed_root else project
    config_path = agent_mode.config_path(args.config_path)

    errors: list[str] = []
    warnings: list[str] = []

    root_result = agent_mode.validate_project_under_allowed_root(project, allowed_root, case_insensitive=case_insensitive)
    errors.extend(root_result.errors)
    warnings.extend(root_result.warnings)
    if agent_mode.path_is_same_or_child(config_path, project, case_insensitive=case_insensitive):
        errors.append("agent config path must live outside the project being exposed")

    scan = agent_mode.scan_project_secrets(project, allow_sensitive_project=args.allow_sensitive_project)
    sanitized = None
    sanitized_mode = (args.sanitized_workspace or agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE).strip().lower()
    if sanitized_mode not in agent_mode.VALID_SANITIZED_WORKSPACE_MODES:
        sanitized_mode = agent_mode.DEFAULT_SANITIZED_WORKSPACE_MODE
    needs_sanitized = (not scan.ok or sanitized_mode == "always") and sanitized_mode != "off" and not args.allow_sensitive_project
    if needs_sanitized and args.dry_run:
        warnings.append("dry run: sanitized workspace would be generated before writing config")
    elif needs_sanitized:
        sanitized = agent_mode.create_sanitized_workspace(
            project,
            mode=sanitized_mode,
            workspace_root_path=args.workspace_root,
            reason="setup generated sanitized review workspace",
        )
        warnings.extend(sanitized.warnings)
        if sanitized.errors:
            errors.extend(scan.errors)
            errors.extend(sanitized.errors)
    elif not scan.ok:
        errors.extend(scan.errors)
    warnings.extend(scan.warnings)

    bridge = agent_mode.check_bridge_executable(
        args.bridge_executable,
        project_dir=project,
        allow_project_bridge=args.allow_project_bridge,
        case_insensitive=case_insensitive,
    )
    warnings.extend(bridge.warnings)
    if not bridge.ok:
        warnings.extend(bridge.errors)

    existing_roots = agent_mode.config_allowed_roots(config_path)
    root_additions = [str(allowed_root)]
    if sanitized and sanitized.workspace_dir:
        root_additions.append(str(agent_mode.resolve_path(sanitized.workspace_dir)))
    merged_roots = agent_mode.merge_roots(existing_roots, root_additions, case_insensitive=case_insensitive)
    can_write = not errors
    wrote = False
    if can_write and not args.dry_run:
        agent_mode.write_agent_config_roots(merged_roots, path=config_path)
        wrote = True

    payload = {
        "ok": can_write,
        "project_dir": str(project),
        "allowed_root": str(allowed_root),
        "config_path": str(config_path),
        "would_write": can_write and not args.dry_run,
        "wrote_config": wrote,
        "allowed_roots": merged_roots,
        "root_validation": root_result.__dict__,
        "secret_scan": scan.to_dict(),
        "sanitized_workspace": sanitized.to_dict() if sanitized else None,
        "bridge": bridge.__dict__,
        "errors": errors,
        "warnings": warnings,
        "auto": args.auto,
        "dry_run": args.dry_run,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_text(payload))
    return 0 if can_write else 2


if __name__ == "__main__":
    raise SystemExit(main())
