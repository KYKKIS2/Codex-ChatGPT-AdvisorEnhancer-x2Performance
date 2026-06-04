#!/usr/bin/env python3
"""Build compact project context packs for advisor calls."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_FILES = (
    "project-profile.md",
    "decisions.json",
    "advisor-lessons.md",
    "open-questions.md",
    "outcomes.json",
)


@dataclass
class CommandCapture:
    command: str
    ok: bool
    exit_code: int | None
    output: str


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def sanitize_text(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")


def truncate(text: str, limit: int) -> str:
    text = sanitize_text(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def read_text(path: Path, limit: int) -> str:
    return truncate(path.read_text(encoding="utf-8", errors="replace"), limit)


def advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def packs_dir(project_dir: Path) -> Path:
    return advisor_dir(project_dir) / "context-packs"


def run_capture(project_dir: Path, command: list[str], limit: int) -> CommandCapture:
    try:
        completed = subprocess.run(
            command,
            cwd=project_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
        )
    except FileNotFoundError:
        return CommandCapture(" ".join(command), False, None, "Command not available.")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        return CommandCapture(" ".join(command), False, None, truncate(output, limit))
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return CommandCapture(" ".join(command), completed.returncode == 0, completed.returncode, truncate(output, limit))


def git_context(project_dir: Path, diff_chars: int) -> dict[str, Any]:
    if not (project_dir / ".git").exists():
        return {"available": False, "reason": "No .git directory found."}
    captures = [
        run_capture(project_dir, ["git", "status", "--short"], diff_chars),
        run_capture(project_dir, ["git", "diff", "--stat"], diff_chars),
        run_capture(project_dir, ["git", "diff", "--check"], diff_chars),
        run_capture(project_dir, ["git", "diff", "--"], diff_chars),
    ]
    return {
        "available": True,
        "captures": [capture.__dict__ for capture in captures],
    }


def file_context(project_dir: Path, paths: list[str], max_chars: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in paths:
        path = (project_dir / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        try:
            relative = str(path.relative_to(project_dir))
        except ValueError:
            relative = str(path)
        item: dict[str, Any] = {"path": relative}
        if not path.exists():
            item["ok"] = False
            item["error"] = "File does not exist."
        elif not path.is_file():
            item["ok"] = False
            item["error"] = "Path is not a file."
        else:
            item["ok"] = True
            item["content"] = read_text(path, max_chars)
        items.append(item)
    return items


def memory_context(project_dir: Path, max_chars: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    root = advisor_dir(project_dir)
    for name in MEMORY_FILES:
        path = root / name
        if not path.exists() or not path.is_file():
            continue
        item: dict[str, Any] = {
            "path": str(path.relative_to(project_dir)),
            "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }
        item["content"] = read_text(path, max_chars)
        items.append(item)
    return items


def build_pack(args: argparse.Namespace, prompt: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "trace_id": args.trace_id,
        "task_id": args.task_id,
        "task": prompt.strip(),
        "draft_or_plan": args.draft.strip() if args.draft else "",
        "constraints": args.constraint,
        "test_failures": args.failure.strip() if args.failure else "",
        "relevant_files": file_context(args.project_dir, args.file, args.max_file_chars),
        "extra_context_files": file_context(args.project_dir, args.context_file, args.max_file_chars),
        "git": git_context(args.project_dir, args.diff_chars) if not args.no_git else {"available": False, "reason": "Disabled by --no-git."},
        "memory": memory_context(args.project_dir, args.memory_chars) if not args.no_memory else [],
    }


def write_pack(project_dir: Path, pack: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = packs_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{stamp}-context-pack.json"
    md_path = out_dir / f"{stamp}-context-pack.md"
    json_path.write_text(json.dumps(pack, indent=2), encoding="utf-8")

    lines = [
        "# Advisor Context Pack",
        "",
        f"Created UTC: {pack['created_utc']}",
        f"Trace ID: {pack['trace_id']}",
        f"Task ID: {pack['task_id']}",
        "",
        "## Task",
        "",
        pack["task"],
        "",
    ]
    if pack["draft_or_plan"]:
        lines.extend(["## Draft Or Plan", "", pack["draft_or_plan"], ""])
    if pack["constraints"]:
        lines.extend(["## Constraints", ""])
        for constraint in pack["constraints"]:
            lines.append(f"- {constraint}")
        lines.append("")
    if pack["test_failures"]:
        lines.extend(["## Test Failures Or Error Output", "", "```text", pack["test_failures"], "```", ""])
    for section, title in (("relevant_files", "Relevant Files"), ("extra_context_files", "Extra Context Files")):
        if pack[section]:
            lines.extend([f"## {title}", ""])
            for item in pack[section]:
                lines.extend([f"### {item['path']}", ""])
                if item.get("ok"):
                    lines.extend(["```text", item.get("content", ""), "```", ""])
                else:
                    lines.extend([f"Unavailable: {item.get('error', 'unknown error')}", ""])
    lines.extend(["## Git Context", ""])
    git = pack["git"]
    if git.get("available"):
        for capture in git.get("captures", []):
            lines.extend([
                f"### `{capture['command']}`",
                "",
                f"Exit code: {capture['exit_code']}",
                "",
                "```text",
                capture["output"],
                "```",
                "",
            ])
    else:
        lines.extend([git.get("reason", "Unavailable."), ""])
    if pack["memory"]:
        lines.extend(["## Advisor Memory Summaries", ""])
        for item in pack["memory"]:
            lines.extend([
                f"### {item['path']}",
                "",
                f"Modified UTC: {item['modified_utc']}",
                "",
                "```text",
                item["content"],
                "```",
                "",
            ])
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    latest_json = advisor_dir(project_dir) / "latest-context-pack.json"
    latest_md = advisor_dir(project_dir) / "latest-context-pack.md"
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return json_path, md_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Task or question. Reads stdin when omitted.")
    parser.add_argument("--draft", help="Current Codex draft, plan, or patch summary.")
    parser.add_argument("--draft-file", help="Read draft/current plan from a file.")
    parser.add_argument("--failure", help="Test failure, traceback, or command output.")
    parser.add_argument("--failure-file", help="Read test failure, traceback, or command output from a file.")
    parser.add_argument("--file", action="append", default=[], help="Relevant file to include. Repeat for multiple files.")
    parser.add_argument("--context-file", action="append", default=[], help="Additional context file to include.")
    parser.add_argument("--constraint", action="append", default=[], help="Constraint to include.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--max-file-chars", type=int, default=12000)
    parser.add_argument("--diff-chars", type=int, default=16000)
    parser.add_argument("--memory-chars", type=int, default=8000)
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON path only.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.project_dir = args.project_dir.resolve()
    args.trace_id = args.trace_id or str(uuid.uuid4())
    args.task_id = args.task_id or str(uuid.uuid4())
    if args.draft_file:
        args.draft = read_text(Path(args.draft_file), args.max_file_chars)
    if args.failure_file:
        args.failure = read_text(Path(args.failure_file), args.max_file_chars)
    if args.draft:
        args.draft = sanitize_text(args.draft)
    if args.failure:
        args.failure = sanitize_text(args.failure)
    prompt = sanitize_text(args.prompt if args.prompt is not None else sys.stdin.read())
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2
    pack = build_pack(args, prompt)
    json_path, md_path = write_pack(args.project_dir, pack)
    if args.json:
        print(json_path)
    else:
        print(f"Context pack saved: {md_path}")
        print(f"Context pack JSON saved: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
