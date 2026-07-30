#!/usr/bin/env python3
"""Add and verify a mechanically read-only tool mode in DevSpace."""

from __future__ import annotations

import argparse
import os
import re
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
SHELL_SYNC_DISABLED_PATCHED = '''    if (config.toolMode !== "codex"
        && config.toolMode !== "readonly"
        && config.disableSyncShell !== true) {
        registerAdvisorAppTool(server, toolNames.shell, {'''
SHELL_SYNC_DISABLED_ENV_PATCHED = '''    if (config.toolMode !== "codex"
        && config.toolMode !== "readonly"
        && process.env.DEVSPACE_DISABLE_SYNC_SHELL !== "true") {
        registerAdvisorAppTool(server, toolNames.shell, {'''
FS_IMPORT_NEEDLE = 'import { readFileSync } from "node:fs";'
FS_IMPORT_PATCHED = 'import { readFileSync, realpathSync } from "node:fs";'
FS_IMPORT_SECURE = 'import { lstatSync, readFileSync, realpathSync } from "node:fs";'
RUNTIME_GUARD_MARKER = "const advisorRegisteredTools = new WeakMap();"
RUNTIME_GUARDS = r'''const advisorRegisteredTools = new WeakMap();
const advisorReadonlyToolNames = new Set(["open_workspace", "read", "grep", "glob", "ls"]);
function registerAdvisorAppTool(server, name, ...args) {
    const names = advisorRegisteredTools.get(server) ?? new Set();
    names.add(name);
    advisorRegisteredTools.set(server, names);
    return registerAppTool(server, name, ...args);
}
function advisorReadonlyExactRoot(config) {
    if (config.toolMode !== "readonly")
        return undefined;
    const pointer = process.env.DEVSPACE_READONLY_EXACT_ROOT_FILE;
    if (!pointer)
        throw new Error("Readonly DevSpace requires DEVSPACE_READONLY_EXACT_ROOT_FILE.");
    const configured = readFileSync(pointer, "utf8").trim();
    if (!configured)
        throw new Error("Readonly DevSpace exact-root pointer is empty.");
    return realpathSync(configured);
}
function assertAdvisorReadonlyOpen(config, path, mode, baseRef) {
    if (config.toolMode !== "readonly")
        return;
    if ((mode ?? "checkout") !== "checkout" || baseRef !== undefined)
        throw new Error("Readonly DevSpace permits checkout mode only.");
    if (realpathSync(path) !== advisorReadonlyExactRoot(config))
        throw new Error("Readonly DevSpace denied a workspace outside the pinned review snapshot.");
}
function assertAdvisorReadonlyWorkspace(config, workspace) {
    if (config.toolMode === "readonly" && realpathSync(workspace.root) !== advisorReadonlyExactRoot(config))
        throw new Error("Readonly DevSpace workspace is no longer the pinned review snapshot.");
}
function assertAdvisorReadonlyToolSurface(server, config) {
    if (config.toolMode !== "readonly")
        return;
    const actual = advisorRegisteredTools.get(server) ?? new Set();
    const mismatch = actual.size !== advisorReadonlyToolNames.size
        || [...actual].some((name) => !advisorReadonlyToolNames.has(name));
    if (mismatch)
        throw new Error(`Readonly DevSpace tool surface mismatch: ${[...actual].sort().join(",")}`);
}
'''
OPEN_HANDLER_NEEDLE = '''    }, async ({ path, mode, baseRef }) => {
        const startedAt = performance.now();
        const { workspace, agentsFiles, availableAgentsFiles } = await workspaces.openWorkspace({ path, mode, baseRef });'''
OPEN_HANDLER_PATCHED = '''    }, async ({ path, mode, baseRef }) => {
        const startedAt = performance.now();
        assertAdvisorReadonlyOpen(config, path, mode, baseRef);
        const { workspace, agentsFiles, availableAgentsFiles } = await workspaces.openWorkspace({ path, mode, baseRef });
        assertAdvisorReadonlyWorkspace(config, workspace);'''
OPEN_HANDLER_SECURE = '''    }, async ({ path, mode, baseRef }) => {
        const startedAt = performance.now();
        assertAdvisorReadonlyOpen(config, path, mode, baseRef);
        assertAdvisorPinnedOpen(config, path, mode, baseRef);
        const { workspace, agentsFiles, availableAgentsFiles } = await workspaces.openWorkspace({ path, mode, baseRef });
        assertAdvisorReadonlyWorkspace(config, workspace);
        assertAdvisorPinnedWorkspace(config, workspace);'''
OPEN_DESCRIPTION_NEEDLE = r'''        description: "Open a local project directory as a coding workspace. Call this once per project folder or worktree before reading, editing, searching, writing, showing changes, or running commands. Reuse the returned workspaceId for later calls in the same folder; do not call open_workspace again unless switching folders/worktrees, changing checkout/worktree mode, the workspaceId is rejected as unknown, or the user explicitly asks to reopen. By default this opens the actual checkout; set mode=\"worktree\" when the user asks for an isolated or parallel coding session. Returns a workspaceId, loaded root project instructions, and nested instruction file paths the model should read before working in those directories.",'''
OPEN_DESCRIPTION_PATCHED = r'''        description: config.toolMode === "readonly"
            ? "Open the one pinned read-only advisor snapshot. Call this exactly once before using read, grep, glob, or ls. Only checkout mode is available."
            : "Open a local project directory as a coding workspace. Call this once per project folder or worktree before reading, editing, searching, writing, showing changes, or running commands. Reuse the returned workspaceId for later calls in the same folder; do not call open_workspace again unless switching folders/worktrees, changing checkout/worktree mode, the workspaceId is rejected as unknown, or the user explicitly asks to reopen. By default this opens the actual checkout; set mode=\"worktree\" when the user asks for an isolated or parallel coding session. Returns a workspaceId, loaded root project instructions, and nested instruction file paths the model should read before working in those directories.",'''
OPEN_SCHEMA_NEEDLE = r'''            mode: z
                .enum(["checkout", "worktree"])
                .optional()
                .describe("Defaults to checkout. Use checkout to work in the actual directory. Use worktree to create an isolated managed Git worktree for parallel work."),
            baseRef: z
                .string()
                .optional()
                .describe("Git ref to base a worktree on. Only used with mode=\"worktree\". Defaults to HEAD."),'''
OPEN_SCHEMA_PATCHED = r'''            ...(config.toolMode === "readonly"
                ? {
                    mode: z
                        .literal("checkout")
                        .optional()
                        .describe("Optional; the read-only advisor permits checkout mode only."),
                }
                : {
                    mode: z
                        .enum(["checkout", "worktree"])
                        .optional()
                        .describe("Defaults to checkout. Use checkout to work in the actual directory. Use worktree to create an isolated managed Git worktree for parallel work."),
                    baseRef: z
                        .string()
                        .optional()
                        .describe("Git ref to base a worktree on. Only used with mode=\"worktree\". Defaults to HEAD."),
                }),'''
WORKSPACE_LOOKUP_NEEDLE = "        const workspace = workspaces.getWorkspace(workspaceId);"
WORKSPACE_LOOKUP_PATCHED = WORKSPACE_LOOKUP_NEEDLE + "\n        assertAdvisorReadonlyWorkspace(config, workspace);"
RETURN_SERVER_NEEDLE = "    return server;\n}"
RETURN_SERVER_PATCHED = "    assertAdvisorReadonlyToolSurface(server, config);\n    return server;\n}"


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
    if path.suffix.lower() in {".ps1", ".cmd", ".bat"} and path.is_file():
        wrapper_text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"node_modules[/\\]@waishnav[/\\]devspace[/\\]dist[/\\]cli\.js", wrapper_text):
            candidate = (path.parent / match.group(0)).parent
            candidates.append(candidate)
    candidates.extend(parent / "dist" for parent in path.parents)
    candidates.append(path.parent / "node_modules" / "@waishnav" / "devspace" / "dist")
    for candidate in candidates:
        if (candidate / "cli.js").is_file() and (candidate / "config.js").is_file() and (candidate / "server.js").is_file():
            return candidate
    raise RuntimeError(f"Could not locate the DevSpace dist directory from {executable!r}.")


def replace_once(text: str, needle: str, replacement: str, label: str) -> tuple[str, bool]:
    if replacement in text:
        return text, False
    if label == "filesystem guard import" and FS_IMPORT_SECURE in text:
        return text, False
    if label == "read-only workspace open guard" and OPEN_HANDLER_SECURE in text:
        return text, False
    if label == "shell tool gate" and (
        SHELL_SYNC_DISABLED_PATCHED in text
        or SHELL_SYNC_DISABLED_ENV_PATCHED in text
    ):
        return text, False
    tracked_replacement = replacement.replace(
        "registerAppTool(server,",
        "registerAdvisorAppTool(server,",
    )
    if tracked_replacement in text:
        return text, False
    count = text.count(needle)
    if count != 1:
        raise RuntimeError(f"DevSpace {label} patch expected one compatible insertion point, found {count}.")
    return text.replace(needle, replacement, 1), True


def verify(config_text: str, server_text: str) -> None:
    tracked_write = WRITE_PATCHED.replace("registerAppTool(server,", "registerAdvisorAppTool(server,")
    tracked_search = SEARCH_PATCHED.replace("registerAppTool(server,", "registerAdvisorAppTool(server,")
    tracked_shell = SHELL_PATCHED.replace("registerAppTool(server,", "registerAdvisorAppTool(server,")
    required = (
        (CONFIG_PATCHED, config_text),
        (INSTRUCTION_PATCH.strip(), server_text),
        (tracked_write, server_text),
        (tracked_search, server_text),
        (RUNTIME_GUARD_MARKER, server_text),
        (OPEN_DESCRIPTION_PATCHED, server_text),
        (OPEN_SCHEMA_PATCHED, server_text),
        (WORKSPACE_LOOKUP_PATCHED, server_text),
        (RETURN_SERVER_PATCHED, server_text),
    )
    missing = [marker.splitlines()[0] for marker, text in required if marker not in text]
    if FS_IMPORT_PATCHED not in server_text and FS_IMPORT_SECURE not in server_text:
        missing.append(FS_IMPORT_PATCHED)
    if OPEN_HANDLER_PATCHED not in server_text and OPEN_HANDLER_SECURE not in server_text:
        missing.append(OPEN_HANDLER_PATCHED.splitlines()[0])
    if (
        tracked_shell not in server_text
        and SHELL_SYNC_DISABLED_PATCHED not in server_text
        and SHELL_SYNC_DISABLED_ENV_PATCHED not in server_text
    ):
        missing.append(tracked_shell.splitlines()[0])
    if missing:
        raise RuntimeError("DevSpace read-only patch is incomplete: " + ", ".join(missing))
    if server_text.count("registerAppTool(server,") != 1:
        raise RuntimeError("DevSpace has untracked tool registrations outside the read-only surface guard.")


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
        (FS_IMPORT_NEEDLE, FS_IMPORT_PATCHED, "filesystem guard import"),
        (OPEN_DESCRIPTION_NEEDLE, OPEN_DESCRIPTION_PATCHED, "read-only open-workspace description"),
        (OPEN_SCHEMA_NEEDLE, OPEN_SCHEMA_PATCHED, "read-only open-workspace schema"),
        (OPEN_HANDLER_NEEDLE, OPEN_HANDLER_PATCHED, "read-only workspace open guard"),
        (RETURN_SERVER_NEEDLE, RETURN_SERVER_PATCHED, "runtime tool-surface assertion"),
    ):
        server_text, did_change = replace_once(server_text, needle, replacement, label)
        changed |= did_change

    if RUNTIME_GUARD_MARKER not in server_text:
        count = server_text.count(INSTRUCTION_NEEDLE)
        if count != 1:
            raise RuntimeError(
                f"DevSpace runtime guard patch expected one insertion point, found {count}."
            )
        server_text = server_text.replace(
            INSTRUCTION_NEEDLE,
            RUNTIME_GUARDS + INSTRUCTION_NEEDLE,
            1,
        )
        server_text = server_text.replace(
            "registerAppTool(server,",
            "registerAdvisorAppTool(server,",
        )
        # Keep the one base registration inside the tracking wrapper.
        server_text = server_text.replace(
            "return registerAdvisorAppTool(server, name, ...args);",
            "return registerAppTool(server, name, ...args);",
            1,
        )
        changed = True

    if WORKSPACE_LOOKUP_PATCHED not in server_text:
        count = server_text.count(WORKSPACE_LOOKUP_NEEDLE)
        if count < 1:
            raise RuntimeError("DevSpace workspace guard patch found no workspace lookup sites.")
        server_text = server_text.replace(
            WORKSPACE_LOOKUP_NEEDLE,
            WORKSPACE_LOOKUP_PATCHED,
        )
        changed = True

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
