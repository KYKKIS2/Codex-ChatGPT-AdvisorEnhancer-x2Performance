#!/usr/bin/env python3
"""Add and verify a mechanically read-only tool mode in DevSpace."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path


CONFIG_NEEDLE = 'if (mode === "minimal" || mode === "full" || mode === "codex")'
CONFIG_PATCHED = 'if (mode === "minimal" || mode === "full" || mode === "codex" || mode === "readonly")'
INSTRUCTION_NEEDLE = "function serverInstructions(config) {\n"
INSTRUCTION_PATCH = """function serverInstructions(config) {
    if (config.toolMode === "readonly") {
        return `Use DevSpace as a read-only local workspace. Call ${toolNames.openWorkspace} exactly once for the requested folder, reuse its workspaceId, and use only ${toolNames.read}, ${toolNames.grep}, ${toolNames.glob}, and ${toolNames.ls}. No shell or mutation tools are available. Follow loaded project instructions and never inspect secret paths.`;
    }
"""
WRITE_NEEDLE = '    if (config.toolMode !== "codex") {\n        registerAppTool(server, toolNames.write, {'
WRITE_PATCHED = '    if (config.toolMode !== "codex" && config.toolMode !== "readonly") {\n        registerAppTool(server, toolNames.write, {'
SEARCH_NEEDLE = '    if (config.toolMode === "full") {\n        registerAppTool(server, toolNames.grep, {'
SEARCH_PATCHED = '    if (config.toolMode === "full" || config.toolMode === "readonly") {\n        registerAppTool(server, toolNames.grep, {'
SHELL_NEEDLE = '    if (config.toolMode !== "codex") {\n        registerAppTool(server, toolNames.shell, {'
SHELL_PATCHED = '    if (config.toolMode !== "codex" && config.toolMode !== "readonly") {\n        registerAppTool(server, toolNames.shell, {'


def atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)


def resolve_dist(executable: str) -> Path:
    resolved = shutil.which(executable) or executable
    path = Path(resolved).expanduser().resolve()
    candidates = [path.parent] if path.name == "cli.js" else []
    candidates.extend(parent / "dist" for parent in path.parents)
    for candidate in candidates:
        if (candidate / "cli.js").is_file() and (candidate / "config.js").is_file() and (candidate / "server.js").is_file():
            return candidate
    raise RuntimeError(f"Could not locate the DevSpace dist directory from {executable!r}.")


def replace_once(text: str, needle: str, replacement: str, label: str) -> tuple[str, bool]:
    if replacement in text:
        return text, False
    count = text.count(needle)
    if count != 1:
        raise RuntimeError(f"DevSpace {label} patch expected one compatible insertion point, found {count}.")
    return text.replace(needle, replacement, 1), True


def verify(config_text: str, server_text: str) -> None:
    required = (
        (CONFIG_PATCHED, config_text),
        (INSTRUCTION_PATCH.strip(), server_text),
        (WRITE_PATCHED, server_text),
        (SEARCH_PATCHED, server_text),
        (SHELL_PATCHED, server_text),
    )
    missing = [marker.splitlines()[0] for marker, text in required if marker not in text]
    if missing:
        raise RuntimeError("DevSpace read-only patch is incomplete: " + ", ".join(missing))


def patch_devspace(dist: Path, *, check_only: bool) -> bool:
    config_path = dist / "config.js"
    server_path = dist / "server.js"
    config_text = config_path.read_text(encoding="utf-8")
    server_text = server_path.read_text(encoding="utf-8")

    if check_only:
        verify(config_text, server_text)
        return False

    changed = False
    config_text, did_change = replace_once(
        config_text,
        CONFIG_NEEDLE,
        CONFIG_PATCHED,
        "tool-mode parser",
    )
    changed |= did_change
    for needle, replacement, label in (
        (INSTRUCTION_NEEDLE, INSTRUCTION_PATCH, "server instructions"),
        (WRITE_NEEDLE, WRITE_PATCHED, "write/edit tool gate"),
        (SEARCH_NEEDLE, SEARCH_PATCHED, "read-only search tool gate"),
        (SHELL_NEEDLE, SHELL_PATCHED, "shell tool gate"),
    ):
        server_text, did_change = replace_once(server_text, needle, replacement, label)
        changed |= did_change

    verify(config_text, server_text)
    if changed:
        atomic_write(config_path, config_text)
        atomic_write(server_path, server_text)
    verify(
        config_path.read_text(encoding="utf-8"),
        server_path.read_text(encoding="utf-8"),
    )
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="devspace")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dist = resolve_dist(args.executable)
        changed = patch_devspace(dist, check_only=args.check)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.check:
        print("DevSpace read-only tool mode verified.")
    elif changed:
        print("DevSpace read-only tool mode applied and verified.")
    else:
        print("DevSpace read-only tool mode already applied and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
