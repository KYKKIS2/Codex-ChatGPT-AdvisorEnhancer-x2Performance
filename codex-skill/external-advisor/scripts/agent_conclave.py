#!/usr/bin/env python3
"""Run a bounded multi-role repo-aware ChatGPT advisor conclave."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import advisor_concurrency as concurrency
import advisor_safety as safety
import conclave


DEFAULT_MAX_WORKERS = 5
DEFAULT_QUEUE_TIMEOUT = 0.0
DEFAULT_TIMEOUT = 0


@dataclass
class AgentRoleResult:
    role: str
    ok: bool
    output: str
    elapsed_seconds: float
    metadata: dict[str, Any]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ok": self.ok,
            "output": self.output,
            "elapsed_seconds": self.elapsed_seconds,
            "metadata": self.metadata,
            "error": self.error,
        }


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def combined_subprocess_timeout(request_timeout: float, queue_timeout: float, cushion: float) -> float | None:
    if request_timeout <= 0 or queue_timeout <= 0:
        return None
    return request_timeout + queue_timeout + cushion


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_project(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.expanduser().resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def run_dir(project: Path, mode: str) -> Path:
    root = project / ".codex-advisor" / "agent-conclave-runs"
    safety.ensure_private_dir(root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}-{uuid.uuid4().hex[:8]}-{safety.safe_slug(mode, default='general')}"
    safety.ensure_private_dir(path)
    return path


def role_task(role: str, mode: str, task: str) -> str:
    return f"""{conclave.ROLE_PROMPTS[role]}

This is a repo-aware {mode} review. Use the connected DevSpace repository tools to inspect only the evidence necessary for this role.

Return:
- repository files inspected
- verified findings with file and line references when available
- recommendations
- risks or uncertainty
- checks Codex should run locally

Do not produce the final user-facing answer.

User task:
{task}
"""


def safe_response_path(project: Path, raw_path: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        path = Path(raw_path).expanduser().resolve()
        path.relative_to(project)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def role_command(args: argparse.Namespace, role: str) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("advisor_agent.py")),
        "--project-dir",
        str(args.project_dir),
        "--role",
        role,
        "--provider",
        args.provider,
        "--base-url",
        args.base_url,
        "--timeout",
        str(args.timeout),
        "--queue-timeout",
        str(args.queue_timeout),
        "--max-output-tokens",
        str(args.max_output_tokens),
        "--json",
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.thinking_effort:
        command.extend(["--thinking-effort", args.thinking_effort])
    if args.allow_shell:
        command.append("--allow-shell")
    if not args.live_activity:
        command.append("--no-live-activity")
    return command


def run_role(args: argparse.Namespace, role: str, task: str) -> AgentRoleResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            role_command(args, role),
            cwd=args.project_dir,
            env=os.environ.copy(),
            input=role_task(role, args.mode, task),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=None,
            timeout=combined_subprocess_timeout(args.timeout, args.queue_timeout, 60),
        )
    except subprocess.TimeoutExpired:
        return AgentRoleResult(role, False, "", time.monotonic() - started, {}, "role timed out")
    except OSError as exc:
        return AgentRoleResult(role, False, "", time.monotonic() - started, {}, f"could not launch role: {exc}")

    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError:
        metadata = {}
    response_path = safe_response_path(args.project_dir, metadata.get("response_path"))
    output = response_path.read_text(encoding="utf-8", errors="replace").strip() if response_path else ""
    errors = metadata.get("errors") if isinstance(metadata.get("errors"), list) else []
    error = "; ".join(str(item) for item in errors[:6])
    ok = completed.returncode == 0 and metadata.get("status") == "ok" and bool(output)
    if not ok and not error:
        error = f"agent role exited with status {completed.returncode}"
    return AgentRoleResult(role, ok, output, time.monotonic() - started, metadata, error)


def run_roles(
    args: argparse.Namespace,
    roles: list[str],
    task: str,
) -> list[AgentRoleResult]:
    if args.parallel and len(roles) > 1:
        workers = min(max(1, args.max_workers), len(roles))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_role, args, role, task): role for role in roles}
            results = [future.result() for future in concurrent.futures.as_completed(futures)]
        order = {role: index for index, role in enumerate(roles)}
        return sorted(results, key=lambda result: order[result.role])
    return [run_role(args, role, task) for role in roles]


def synthesis_prompt(task: str, role_results: list[AgentRoleResult]) -> str:
    blocks = [
        "You are the synthesis advisor for Codex.",
        "Do not use tools or claim additional repository inspection. Synthesize only the supplied repo-aware specialist reports.",
        "Keep disagreements and uncertainty visible. Return the strongest recommendation, important risks, concrete next actions, and what Codex must still verify locally.",
        f"Original task:\n{task}",
        "Repo-aware specialist reports:",
    ]
    for result in role_results:
        status = "ok" if result.ok else "failed"
        body = result.output if result.output else result.error
        blocks.append(f"## {result.role} ({status})\n{body}")
    return "\n\n---\n\n".join(blocks)


def synthesis_command(args: argparse.Namespace, response_path: Path) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("advisor.py")),
        "--provider",
        args.provider,
        "--timeout",
        str(args.timeout),
        "--save",
        str(response_path),
        "--no-live-activity",
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.thinking_effort:
        command.extend(["--thinking-effort", args.thinking_effort])
    return command


def run_synthesis(
    args: argparse.Namespace,
    task: str,
    role_results: list[AgentRoleResult],
    output_dir: Path,
) -> tuple[bool, str, str]:
    response_path = output_dir / "synthesis.md"
    env = os.environ.copy()
    env["ADVISOR_PROJECT_DIR"] = str(args.project_dir)
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_BASE_URL"] = args.base_url
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(max(args.max_output_tokens, 1800))
    env["ADVISOR_QUEUE_TIMEOUT"] = str(args.queue_timeout)
    env["ADVISOR_STATE_PATH"] = str(output_dir / "synthesis.conversation.json")
    env["ADVISOR_RESPONSE_PATH"] = str(response_path)
    env["ADVISOR_AUTO_CREATE_PROJECT"] = "false"
    try:
        completed = subprocess.run(
            synthesis_command(args, response_path),
            cwd=args.project_dir,
            env=env,
            input=synthesis_prompt(task, role_results),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=None,
            timeout=combined_subprocess_timeout(args.timeout, args.queue_timeout, 30),
        )
    except subprocess.TimeoutExpired:
        return False, "", "synthesizer timed out"
    except OSError as exc:
        return False, "", f"could not launch synthesizer: {exc}"
    output = completed.stdout.strip()
    if output:
        safety.atomic_write_text(response_path, output.rstrip() + "\n")
    if completed.returncode != 0:
        return False, output, f"synthesizer exited with status {completed.returncode}"
    if not output:
        return False, "", "synthesizer returned no text"
    return True, output, ""


def write_report(
    output_dir: Path,
    *,
    task: str,
    mode: str,
    role_results: list[AgentRoleResult],
    synthesis: str,
) -> Path:
    lines = [
        "# Repo-Aware Advisor Agent Conclave",
        "",
        f"Mode: {mode}",
        f"Created UTC: {utc_now()}",
        "",
        "## Task",
        "",
        task,
        "",
        "## Synthesis",
        "",
        synthesis or "Synthesis skipped or unavailable.",
        "",
        "## Specialist Reports",
        "",
    ]
    for result in role_results:
        lines.extend(
            [
                f"### {result.role} ({'ok' if result.ok else 'failed'}, {result.elapsed_seconds:.1f}s)",
                "",
                result.output or result.error or "No output.",
                "",
            ]
        )
    path = output_dir / "report.md"
    safety.atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    return path


def publish_latest_reports(project: Path, report_text: str, *, successful: bool) -> None:
    state_dir = project / ".codex-advisor"
    safety.ensure_private_dir(state_dir)
    safety.atomic_write_text(state_dir / "latest-agent-conclave-attempt.md", report_text)
    if successful:
        safety.atomic_write_text(state_dir / "latest-agent-conclave.md", report_text)


def parse_roles(raw: str | None, mode: str) -> list[str]:
    roles = [safety.safe_slug(item.strip().lower()) for item in raw.split(",")] if raw else list(conclave.MODE_ROLES[mode])
    unknown = [role for role in roles if role not in conclave.ROLE_PROMPTS or role == "synthesizer"]
    if unknown:
        raise ValueError("unknown or invalid role(s): " + ", ".join(unknown))
    return roles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Repo-aware conclave task. Reads stdin when omitted.")
    parser.add_argument("--mode", choices=sorted(conclave.MODE_ROLES), default="general")
    parser.add_argument("--roles", help="Comma-separated specialist role override.")
    parser.add_argument("--project-dir", type=Path, help="Original project directory.")
    parser.add_argument("--provider", choices=["openai", "openai-compatible"], default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"))
    parser.add_argument("--base-url", default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL"))
    parser.add_argument(
        "--thinking-effort",
        default=(
            os.environ.get("ADVISOR_THINKING_EFFORT")
            or os.environ.get("ADVISOR_CHATGPT_THINKING_EFFORT")
            or os.environ.get("ADVISOR_INTELLIGENCE")
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("ADVISOR_AGENT_TIMEOUT", str(DEFAULT_TIMEOUT))),
        help="Maximum completion wait per role; 0 waits until ChatGPT finishes.",
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=float(os.environ.get("ADVISOR_QUEUE_TIMEOUT", str(DEFAULT_QUEUE_TIMEOUT))),
        help="Maximum seconds each role or synthesis may wait for coordination; 0 waits until available.",
    )
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1600")))
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--parallel", dest="parallel", action="store_true")
    execution.add_argument("--serial", dest="parallel", action="store_false")
    parser.set_defaults(parallel=True)
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Deprecated safety diagnostic; normal repo-aware roles do not expose shell tools.",
    )
    parser.add_argument("--allow-partial", action="store_true", help="Synthesize successful roles even when another role fails.")
    parser.add_argument("--no-synthesis", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    activity = parser.add_mutually_exclusive_group()
    activity.add_argument("--live-activity", dest="live_activity", action="store_true")
    activity.add_argument("--no-live-activity", dest="live_activity", action="store_false")
    parser.set_defaults(live_activity=True)
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.project_dir = resolve_project(args.project_dir)
    task = safety.redact_sensitive_text(
        safety.sanitize_text(args.prompt if args.prompt is not None else sys.stdin.read())
    ).strip()
    if not task:
        print("Provide --prompt or pipe task text on stdin.", file=sys.stderr)
        return 2
    try:
        roles = parse_roles(args.roles, args.mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.max_workers < 1:
        print("--max-workers must be at least 1.", file=sys.stderr)
        return 2
    if args.timeout < 0:
        print("--timeout cannot be negative; use 0 to wait until each remote turn finishes.", file=sys.stderr)
        return 2
    if args.queue_timeout < 0:
        print("--queue-timeout cannot be negative.", file=sys.stderr)
        return 2
    effective_timeout = concurrency.effective_agent_timeout(args.timeout)
    if effective_timeout != args.timeout:
        print(
            "Advisor ignored the legacy 900-second agent cutoff and will wait for each final "
            "ChatGPT turn. Set ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT=true only for a deliberate "
            "bounded diagnostic.",
            file=sys.stderr,
        )
        args.timeout = effective_timeout
    if args.allow_shell:
        print(
            "--allow-shell is disabled: the repo-aware advisor connector is mechanically read-only.",
            file=sys.stderr,
        )
        return 2

    output_dir = run_dir(args.project_dir, args.mode)
    started = time.monotonic()
    if args.dry_run:
        role_results = [
            AgentRoleResult(role, True, f"[dry-run] Would launch repo-aware {role}.", 0.0, {"status": "dry-run"})
            for role in roles
        ]
    else:
        role_results = run_roles(args, roles, task)

    failed_roles = [result.role for result in role_results if not result.ok]
    synthesis_ok = True
    synthesis = "Synthesis skipped."
    synthesis_error = ""
    can_synthesize = not failed_roles or args.allow_partial
    successful_roles = [result for result in role_results if result.ok]
    if not args.no_synthesis and not args.dry_run and can_synthesize and successful_roles:
        synthesis_ok, synthesis, synthesis_error = run_synthesis(args, task, successful_roles, output_dir)
    elif not args.no_synthesis and args.dry_run:
        synthesis = "[dry-run] Would synthesize repo-aware specialist reports without additional tool access."
    elif failed_roles and not args.allow_partial:
        synthesis_ok = False
        synthesis = "Synthesis skipped because one or more required specialist roles failed."

    report_path = write_report(
        output_dir,
        task=task,
        mode=args.mode,
        role_results=role_results,
        synthesis=synthesis,
    )
    payload = {
        "schema_version": "1.0",
        "created_utc": utc_now(),
        "status": "ok" if not failed_roles and synthesis_ok else "failed",
        "project_dir": str(args.project_dir),
        "mode": args.mode,
        "roles": roles,
        "parallel": args.parallel,
        "max_workers": args.max_workers,
        "request_timeout_seconds": args.timeout,
        "queue_timeout_seconds": args.queue_timeout,
        "allow_shell": args.allow_shell,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "role_results": [result.to_dict() for result in role_results],
        "failed_roles": failed_roles,
        "synthesis": {
            "ok": synthesis_ok,
            "output": synthesis,
            "error": synthesis_error,
        },
        "report_path": str(report_path),
        "run_dir": str(output_dir),
    }
    metadata_path = output_dir / "meta.json"
    safety.atomic_write_json(metadata_path, payload)
    report_text = report_path.read_text(encoding="utf-8")
    publish_latest_reports(
        args.project_dir,
        report_text,
        successful=payload["status"] == "ok",
    )

    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.no_synthesis:
        for result in role_results:
            print(f"## {result.role} ({'ok' if result.ok else 'failed'})\n")
            print(result.output or result.error)
            print()
        print(f"Agent conclave report saved: {report_path}", file=sys.stderr)
    else:
        print(synthesis)
        print(f"\nAgent conclave report saved: {report_path}", file=sys.stderr)
        print(f"Agent conclave metadata saved: {metadata_path}", file=sys.stderr)

    if failed_roles:
        print("Repo-aware specialist role failures: " + ", ".join(failed_roles), file=sys.stderr)
    if synthesis_error:
        print("Repo-aware synthesis failure: " + synthesis_error, file=sys.stderr)
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
