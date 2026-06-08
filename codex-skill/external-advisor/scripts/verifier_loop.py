#!/usr/bin/env python3
"""Run an evidence-backed verifier loop for Codex plans and patches."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_PREFIXES = (
    "python ",
    "python3 ",
    "py ",
    "pytest",
    "ruff",
    "npm test",
    "npm run ",
    "pnpm test",
    "pnpm run ",
    "yarn test",
    "yarn run ",
    "go test",
    "cargo test",
    "dotnet test",
    "mvn test",
    "gradle test",
    "git status",
    "git diff",
    "git ls-files",
)

DANGEROUS_PATTERNS = (
    r"\brm\b",
    r"\bdel\b",
    r"\berase\b",
    r"\brmdir\b",
    r"\brd\b",
    r"\bremove-item\b",
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bdd\b",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"\bgit\s+reset\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+checkout\b",
    r"[;&|<>`]",
)


@dataclass
class CommandResult:
    command: str
    status: str
    exit_code: int | None
    elapsed_seconds: float
    stdout: str
    stderr: str
    reason: str = ""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def sanitize_text(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")


def read_text(path: str) -> str:
    return sanitize_text(Path(path).read_text(encoding="utf-8"))


def advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def verifier_runs_dir(project_dir: Path) -> Path:
    return advisor_dir(project_dir) / "verifier-runs"


def conclave_script_path() -> Path:
    return Path(__file__).resolve().with_name("conclave.py")


def truncate(text: str, limit: int) -> str:
    text = sanitize_text(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def build_checklist_prompt(args: argparse.Namespace, prompt: str) -> str:
    blocks = [
        "Create an evidence checklist for this Codex plan, patch, or answer.",
        "Return verification commands/checks that would prove or reject the work. Prefer fast local tests and inspections.",
        f"User task:\n{prompt.strip()}",
    ]
    if args.draft:
        blocks.append(f"Codex draft/current plan:\n{args.draft.strip()}")
    for path in args.context_file:
        blocks.append(f"Context file: {path}\n{read_text(path)}")
    blocks.append(
        "Important: Put runnable commands under verification.commands. "
        "Avoid destructive commands. Include expected signals."
    )
    return "\n\n---\n\n".join(block for block in blocks if block.strip())


def build_interpretation_prompt(
    args: argparse.Namespace,
    prompt: str,
    checklist: dict[str, Any] | None,
    command_results: list[CommandResult],
) -> str:
    blocks = [
        "Interpret real verification evidence for Codex.",
        "Say whether the plan, patch, or answer is supported, unsupported, or still uncertain.",
        "Recommend the smallest next action. Do not produce the final user-facing answer.",
        f"User task:\n{prompt.strip()}",
    ]
    if args.draft:
        blocks.append(f"Codex draft/current plan:\n{args.draft.strip()}")
    if checklist:
        blocks.append("Initial verifier checklist JSON:\n" + json.dumps(checklist, indent=2))
    evidence = []
    for result in command_results:
        evidence.append(
            "\n".join([
                f"Command: {result.command}",
                f"Status: {result.status}",
                f"Exit code: {result.exit_code}",
                f"Elapsed seconds: {result.elapsed_seconds:.1f}",
                f"Reason: {result.reason}",
                "STDOUT:",
                truncate(result.stdout, args.output_chars),
                "STDERR:",
                truncate(result.stderr, args.output_chars),
            ])
        )
    blocks.append("Actual command evidence:\n" + "\n\n--- command ---\n\n".join(evidence))
    blocks.append(
        "Return JSON with recommendation, confidence, risks, evidence, next_actions, "
        "and verification.expected_signals updated from the real command output."
    )
    return "\n\n---\n\n".join(block for block in blocks if block.strip())


def run_conclave_verifier(args: argparse.Namespace, phase: str, prompt: str) -> tuple[dict[str, Any] | None, Path | None, str]:
    if args.dry_run:
        parsed = {
            "schema_version": "1.0",
            "role": "verifier",
            "task_type": "verification",
            "recommendation": f"[dry-run] {phase} verifier result.",
            "confidence": 1.0,
            "confidence_reason": "Dry run.",
            "assumptions": [],
            "risks": [],
            "evidence": [],
            "next_actions": [],
            "verification": {
                "commands": args.command,
                "checks": ["Inspect command outputs."],
                "expected_signals": ["Commands finish with expected exit codes."]
            },
            "escalate": False,
            "escalation_reason": "",
        }
        return parsed, None, json.dumps(parsed, indent=2)

    env = os.environ.copy()
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_BASE_URL"] = args.base_url
    env["ADVISOR_MODEL"] = args.model
    env["ADVISOR_REASONING_EFFORT"] = args.reasoning_effort
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)

    task_id = f"{args.task_id}-{phase}"
    command = [
        sys.executable,
        str(conclave_script_path()),
        "--provider", args.provider,
        "--model", args.model,
        "--timeout", str(args.timeout),
        "--mode", "verification",
        "--roles", "verifier",
        "--machine-json",
        "--no-synthesis",
        "--trace-id", args.trace_id,
        "--task-id", task_id,
    ]
    if args.no_sync:
        command.append("--no-sync")
    completed = subprocess.run(
        command,
        cwd=args.project_dir,
        env=env,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=args.timeout + 30,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    path = find_conclave_run(args.project_dir, args.trace_id, task_id)
    parsed = extract_parsed_verifier(path) if path else None
    if completed.returncode != 0:
        raise RuntimeError(f"Verifier advisor phase '{phase}' failed:\n{output}")
    return parsed, path, output


def find_conclave_run(project_dir: Path, trace_id: str, task_id: str) -> Path | None:
    runs_dir = advisor_dir(project_dir) / "conclave-runs"
    if not runs_dir.exists():
        return None
    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("trace_id") == trace_id and data.get("task_id") == task_id:
            return path
    return None


def extract_parsed_verifier(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    for result in data.get("role_results", []):
        if result.get("role") == "verifier" and isinstance(result.get("parsed"), dict):
            return result["parsed"]
    return None


def suggested_commands(checklist: dict[str, Any] | None) -> list[str]:
    if not checklist:
        return []
    verification = checklist.get("verification")
    if not isinstance(verification, dict):
        return []
    commands = verification.get("commands")
    if not isinstance(commands, list):
        return []
    return [str(command).strip() for command in commands if str(command).strip()]


def command_is_safe(command: str) -> tuple[bool, str]:
    lowered = command.strip().lower()
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, lowered):
            return False, f"Rejected by safety pattern: {pattern}"
    if any(lowered == prefix.strip() or lowered.startswith(prefix) for prefix in SAFE_PREFIXES):
        return True, "Allowed safe command prefix."
    return False, "Command prefix is not in the default safe allowlist."


def run_command(args: argparse.Namespace, command: str) -> CommandResult:
    safe, reason = command_is_safe(command)
    if not safe and not args.allow_unsafe_commands:
        return CommandResult(command, "skipped", None, 0.0, "", "", reason)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=args.project_dir,
            shell=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=args.command_timeout,
        )
        return CommandResult(
            command,
            "completed",
            completed.returncode,
            time.monotonic() - started,
            truncate(completed.stdout, args.output_chars),
            truncate(completed.stderr, args.output_chars),
            reason,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command,
            "timeout",
            None,
            time.monotonic() - started,
            truncate(exc.stdout or "", args.output_chars),
            truncate(exc.stderr or "", args.output_chars),
            f"Timed out after {args.command_timeout}s.",
        )
    except Exception as exc:
        return CommandResult(command, "error", None, time.monotonic() - started, "", str(exc), reason)


def write_loop_run(
    args: argparse.Namespace,
    prompt: str,
    checklist: dict[str, Any] | None,
    checklist_path: Path | None,
    command_results: list[CommandResult],
    interpretation: dict[str, Any] | None,
    interpretation_path: Path | None,
) -> tuple[Path, Path]:
    runs_dir = verifier_runs_dir(args.project_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = runs_dir / f"{stamp}-verifier-loop"
    payload = {
        "schema_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "trace_id": args.trace_id,
        "task_id": args.task_id,
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "prompt": prompt.strip(),
        "draft": args.draft.strip() if args.draft else "",
        "checklist_conclave_json": str(checklist_path) if checklist_path else "",
        "interpretation_conclave_json": str(interpretation_path) if interpretation_path else "",
        "checklist": checklist,
        "command_results": [asdict(result) for result in command_results],
        "interpretation": interpretation,
    }
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Advisor Verifier Loop",
        "",
        f"Created UTC: {payload['created_utc']}",
        f"Trace ID: {args.trace_id}",
        f"Task ID: {args.task_id}",
        f"Model: {args.model}",
        "",
        "## Prompt",
        "",
        prompt.strip(),
        "",
        "## Checklist",
        "",
        "```json",
        json.dumps(checklist, indent=2),
        "```",
        "",
        "## Command Evidence",
        "",
    ]
    for result in command_results:
        lines.extend([
            f"### `{result.command}`",
            "",
            f"Status: {result.status}",
            f"Exit code: {result.exit_code}",
            f"Reason: {result.reason}",
            "",
            "STDOUT:",
            "",
            "```text",
            result.stdout,
            "```",
            "",
            "STDERR:",
            "",
            "```text",
            result.stderr,
            "```",
            "",
        ])
    lines.extend([
        "## Verifier Interpretation",
        "",
        "```json",
        json.dumps(interpretation, indent=2),
        "```",
        "",
    ])
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    latest_json = advisor_dir(args.project_dir) / "latest-verifier-loop.json"
    latest_md = advisor_dir(args.project_dir) / "latest-verifier-loop.md"
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return json_path, md_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Task, answer, plan, or patch to verify. Reads stdin when omitted.")
    parser.add_argument("--draft", help="Codex draft/current plan/patch.")
    parser.add_argument("--draft-file", help="Read Codex draft/current plan/patch from a file.")
    parser.add_argument("--context-file", action="append", default=[], help="Additional UTF-8 context file.")
    parser.add_argument("--command", action="append", default=[], help="Evidence command to run. Repeat for multiple commands.")
    parser.add_argument("--no-run-suggested", action="store_true", default=True, help="Only run commands passed with --command. This is the default.")
    parser.add_argument("--run-suggested", dest="no_run_suggested", action="store_false", help="Also run allowed commands suggested by the verifier advisor.")
    parser.add_argument("--allow-unsafe-commands", action="store_true", help="Run commands even when they fail the safe allowlist.")
    parser.add_argument("--provider", choices=["openai", "openai-compatible"], default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"))
    parser.add_argument("--base-url", default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL", "gpt-5-5-thinking"))
    parser.add_argument("--reasoning-effort", default=os.environ.get("ADVISOR_REASONING_EFFORT", "high"))
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1200")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--command-timeout", type=int, default=120)
    parser.add_argument("--output-chars", type=int, default=6000)
    parser.add_argument("--project-dir", type=Path, help="Project directory. Defaults to the nearest Git repo root or current directory.")
    parser.add_argument("--trace-id", default=os.environ.get("ADVISOR_TRACE_ID"))
    parser.add_argument("--task-id", default=os.environ.get("ADVISOR_TASK_ID"))
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Use fake verifier advisor responses but still run allowed evidence commands.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.project_dir = resolve_project_dir(args.project_dir)
    args.trace_id = args.trace_id or str(uuid.uuid4())
    args.task_id = args.task_id or str(uuid.uuid4())
    if args.draft_file:
        args.draft = read_text(args.draft_file)
    elif args.draft:
        args.draft = sanitize_text(args.draft)
    prompt = sanitize_text(args.prompt if args.prompt is not None else sys.stdin.read())
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2

    checklist_prompt = build_checklist_prompt(args, prompt)
    checklist, checklist_path, _ = run_conclave_verifier(args, "checklist", checklist_prompt)
    commands = list(args.command)
    if not args.no_run_suggested:
        for command in suggested_commands(checklist):
            if command not in commands:
                commands.append(command)
    if not commands:
        commands = ["git status --short", "git diff --check"]

    command_results = [run_command(args, command) for command in commands]
    interpretation_prompt = build_interpretation_prompt(args, prompt, checklist, command_results)
    interpretation, interpretation_path, _ = run_conclave_verifier(args, "interpretation", interpretation_prompt)
    json_path, md_path = write_loop_run(
        args,
        prompt,
        checklist,
        checklist_path,
        command_results,
        interpretation,
        interpretation_path,
    )

    print("Verifier loop complete.")
    print(f"Commands completed: {sum(1 for result in command_results if result.status == 'completed')}")
    print(f"Commands skipped: {sum(1 for result in command_results if result.status == 'skipped')}")
    if interpretation:
        print(f"Recommendation: {interpretation.get('recommendation', '')}")
        print(f"Confidence: {interpretation.get('confidence', '')}")
    print(f"Verifier loop saved: {md_path}")
    print(f"Verifier loop JSON saved: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
