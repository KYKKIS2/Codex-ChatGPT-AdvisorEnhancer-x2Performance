#!/usr/bin/env python3
"""Run a bounded multi-role repo-aware ChatGPT advisor conclave."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
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
import advisor_agent
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


def validated_resume_dir(project: Path, raw: Path) -> Path:
    root = (project / ".codex-advisor" / "agent-conclave-runs").resolve()
    path = raw.expanduser().resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Agent conclave resume paths must stay under .codex-advisor/agent-conclave-runs.") from exc
    if not relative.parts or not path.is_dir():
        raise RuntimeError("The requested agent conclave run directory does not exist.")
    return path


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def role_run_specs(output_dir: Path, roles: list[str]) -> dict[str, dict[str, str]]:
    specs: dict[str, dict[str, str]] = {}
    for role in roles:
        role_dir = output_dir / "roles" / safety.safe_slug(role, default="reviewer")
        safety.ensure_private_dir(role_dir)
        specs[role] = {
            "run_dir": str(role_dir),
            "recovery_token": f"ADVISOR-AGENT-{uuid.uuid4().hex.upper()}-COMPLETE",
            "status": "pending",
        }
    return specs


def write_initial_manifest(
    output_dir: Path,
    *,
    args: argparse.Namespace,
    roles: list[str],
    task: str,
    specs: dict[str, dict[str, str]],
) -> Path:
    path = output_dir / "manifest.json"
    payload = {
        "schema_version": "2.0",
        "created_utc": utc_now(),
        "updated_utc": utc_now(),
        "status": "running",
        "project_dir": str(args.project_dir),
        "mode": args.mode,
        "task": task,
        "roles": roles,
        "parallel": args.parallel,
        "max_workers": args.max_workers,
        "provider": args.provider,
        "base_url": args.base_url,
        "model": args.model or "",
        "thinking_effort": args.thinking_effort or "",
        "request_timeout_seconds": args.timeout,
        "queue_timeout_seconds": args.queue_timeout,
        "max_output_tokens": args.max_output_tokens,
        "allow_partial": args.allow_partial,
        "no_synthesis": args.no_synthesis,
        "live_activity": args.live_activity,
        "role_runs": specs,
    }
    safety.atomic_write_json(path, payload)
    return path


def checkpoint_role(manifest_path: Path, result: AgentRoleResult) -> None:
    with concurrency.InterProcessLock(manifest_path.with_suffix(".lock"), timeout=30.0):
        payload = read_json_object(manifest_path)
        role_runs = payload.get("role_runs")
        if not isinstance(role_runs, dict):
            return
        role_state = role_runs.get(result.role)
        if not isinstance(role_state, dict):
            return
        role_state.update(
            {
                "status": "ok" if result.ok else str(result.metadata.get("status") or "failed"),
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "error": result.error,
                "updated_utc": utc_now(),
            }
        )
        payload["updated_utc"] = utc_now()
        safety.atomic_write_json(manifest_path, payload)


def result_from_role_dir(project: Path, role: str, role_dir: Path) -> AgentRoleResult | None:
    metadata = read_json_object(role_dir / "meta.json")
    if not metadata:
        return None
    response_path = safe_response_path(project, metadata.get("response_path"))
    output = response_path.read_text(encoding="utf-8", errors="replace").strip() if response_path else ""
    errors = metadata.get("errors") if isinstance(metadata.get("errors"), list) else []
    error = "; ".join(str(item) for item in errors[:6])
    ok = metadata.get("status") == "ok" and bool(output)
    if not ok and not error:
        error = str(metadata.get("resume_detail") or f"agent role status is {metadata.get('status', 'unknown')}")
    return AgentRoleResult(
        role=role,
        ok=ok,
        output=output,
        elapsed_seconds=float(metadata.get("elapsed_seconds") or 0.0),
        metadata=metadata,
        error=error,
    )


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


def role_command(
    args: argparse.Namespace,
    role: str,
    spec: dict[str, str],
    *,
    resume: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("advisor_agent.py")),
        "--project-dir",
        str(args.project_dir),
    ]
    if resume:
        command.extend(
            [
                "--resume-run-dir",
                spec["run_dir"],
                "--timeout",
                str(args.timeout),
                "--json",
            ]
        )
        return command
    command.extend(
        [
        "--role",
        role,
        "--run-dir",
        spec["run_dir"],
        "--recovery-token",
        spec["recovery_token"],
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
    )
    if args.model:
        command.extend(["--model", args.model])
    if args.thinking_effort:
        command.extend(["--thinking-effort", args.thinking_effort])
    if args.allow_shell:
        command.append("--allow-shell")
    if not args.live_activity:
        command.append("--no-live-activity")
    return command


def run_role(
    args: argparse.Namespace,
    role: str,
    task: str,
    spec: dict[str, str],
    *,
    resume: bool = False,
) -> AgentRoleResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            role_command(args, role, spec, resume=resume),
            cwd=args.project_dir,
            env=os.environ.copy(),
            input=None if resume else role_task(role, args.mode, task),
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
        error = str(metadata.get("resume_detail") or f"agent role exited with status {completed.returncode}")
    return AgentRoleResult(role, ok, output, time.monotonic() - started, metadata, error)


def run_roles(
    args: argparse.Namespace,
    roles: list[str],
    task: str,
    specs: dict[str, dict[str, str]],
    manifest_path: Path,
) -> list[AgentRoleResult]:
    if args.parallel and len(roles) > 1:
        workers = min(max(1, args.max_workers), len(roles))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(run_role, args, role, task, specs[role]): role
                for role in roles
            }
            results: list[AgentRoleResult] = []
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                checkpoint_role(manifest_path, result)
                results.append(result)
        order = {role: index for index, role in enumerate(roles)}
        return sorted(results, key=lambda result: order[result.role])
    results = []
    for role in roles:
        result = run_role(args, role, task, specs[role])
        checkpoint_role(manifest_path, result)
        results.append(result)
    return results


def resume_roles(
    args: argparse.Namespace,
    roles: list[str],
    task: str,
    specs: dict[str, dict[str, str]],
    manifest_path: Path,
) -> list[AgentRoleResult]:
    """Recover submitted turns first; launch only roles proven unsubmitted."""
    results: list[AgentRoleResult] = []
    for role in roles:
        spec = specs[role]
        role_dir = Path(spec["run_dir"])
        existing = result_from_role_dir(args.project_dir, role, role_dir)
        if existing is not None and existing.ok:
            checkpoint_role(manifest_path, existing)
            results.append(existing)
            continue

        request_exists = (role_dir / "request.json").is_file()
        recovered: AgentRoleResult | None = None
        if request_exists:
            recovered = run_role(args, role, task, spec, resume=True)
            checkpoint_role(manifest_path, recovered)
            if recovered.ok:
                results.append(recovered)
                continue
            recovery_status = str(recovered.metadata.get("status") or "")
            safe_to_submit = recovered.metadata.get("safe_to_submit") is True
            if recovery_status != "not-submitted" or not safe_to_submit:
                results.append(recovered)
                continue

        launched = run_role(args, role, task, spec)
        checkpoint_role(manifest_path, launched)
        results.append(launched)
    return results


def synthesis_prompt(
    task: str,
    role_results: list[AgentRoleResult],
    marker: str | None = None,
) -> str:
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
    if marker:
        blocks.append(f"Finish with this exact marker on its own line:\n{marker}")
    return "\n\n---\n\n".join(blocks)


def synthesis_input_sha256(task: str, role_results: list[AgentRoleResult]) -> str:
    prompt = synthesis_prompt(task, role_results)
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def synthesis_checkpoint_dir(
    output_dir: Path,
    task: str,
    role_results: list[AgentRoleResult],
) -> tuple[Path, str]:
    fingerprint = synthesis_input_sha256(task, role_results)
    path = output_dir / "syntheses" / fingerprint[:24]
    safety.ensure_private_dir(path)
    return path, fingerprint


def publish_synthesis_checkpoint(
    output_dir: Path,
    checkpoint_dir: Path,
    fingerprint: str,
    output: str,
    *,
    source: str,
) -> None:
    checkpoint_response = checkpoint_dir / "response.md"
    safety.atomic_write_text(checkpoint_response, output.rstrip() + "\n")
    safety.atomic_write_text(output_dir / "synthesis.md", output.rstrip() + "\n")
    metadata = {
        "schema_version": "1.1",
        "created_utc": utc_now(),
        "status": "ok",
        "input_sha256": fingerprint,
        "checkpoint_dir": str(checkpoint_dir),
        "response_path": str(checkpoint_response),
        "response_source": source,
    }
    safety.atomic_write_json(checkpoint_dir / "meta.json", metadata)
    safety.atomic_write_json(output_dir / "synthesis.meta.json", metadata)


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
    checkpoint_dir, input_sha256 = synthesis_checkpoint_dir(output_dir, task, role_results)
    response_path = checkpoint_dir / "response.md"
    state_path = checkpoint_dir / "conversation.json"
    journal_path = checkpoint_dir / "turn-journal.json"
    request_path = checkpoint_dir / "request.json"
    existing_journal = read_json_object(journal_path)
    if read_json_object(request_path) and advisor_agent.journal_proves_submission(existing_journal):
        return False, "", "synthesis may already have been submitted; resume the conclave instead of posting again"
    marker = f"ADVISOR-SYNTHESIS-{uuid.uuid4().hex.upper()}-COMPLETE"
    prompt = synthesis_prompt(task, role_results, marker)
    env = os.environ.copy()
    env["ADVISOR_PROJECT_DIR"] = str(args.project_dir)
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_BASE_URL"] = args.base_url
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(max(args.max_output_tokens, 1800))
    env["ADVISOR_QUEUE_TIMEOUT"] = str(args.queue_timeout)
    env["ADVISOR_STATE_PATH"] = str(state_path)
    env["ADVISOR_RESPONSE_PATH"] = str(response_path)
    env["ADVISOR_TURN_JOURNAL_PATH"] = str(journal_path)
    env["ADVISOR_AUTO_CREATE_PROJECT"] = "false"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    safety.atomic_write_json(
        request_path,
        {
            "schema_version": "1.0",
            "created_utc": utc_now(),
            "status": "ready-to-submit",
            "project_dir": str(args.project_dir),
            "input_sha256": input_sha256,
            "checkpoint_dir": str(checkpoint_dir),
            "prompt": prompt,
            "marker": marker,
            "state_path": str(state_path),
            "journal_path": str(journal_path),
            "response_path": str(response_path),
            "chatgpt_project_id": advisor.chatgpt_project_id(allow_create=False) or "",
        },
    )
    try:
        completed = subprocess.run(
            synthesis_command(args, response_path),
            cwd=args.project_dir,
            env=env,
            input=prompt,
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
    if completed.returncode != 0:
        return False, output, f"synthesizer exited with status {completed.returncode}"
    if not output:
        return False, "", "synthesizer returned no text"
    if marker not in output:
        return False, output, "synthesizer final response omitted its completion marker"
    output = advisor_agent.strip_completion_marker(output, marker)
    publish_synthesis_checkpoint(
        output_dir,
        checkpoint_dir,
        input_sha256,
        output,
        source="advisor-transport",
    )
    return True, output, ""


def recover_synthesis(
    args: argparse.Namespace,
    output_dir: Path,
    task: str,
    role_results: list[AgentRoleResult],
) -> tuple[str, str, str]:
    """Recover a submitted synthesis with GET-only requests."""
    checkpoint_dir, input_sha256 = synthesis_checkpoint_dir(output_dir, task, role_results)
    response_path = checkpoint_dir / "response.md"
    metadata_path = checkpoint_dir / "meta.json"
    existing = read_json_object(metadata_path)
    if (
        existing.get("status") == "ok"
        and existing.get("input_sha256") == input_sha256
        and response_path.is_file()
    ):
        output = response_path.read_text(encoding="utf-8", errors="replace").strip()
        if output:
            publish_synthesis_checkpoint(
                output_dir,
                checkpoint_dir,
                input_sha256,
                output,
                source=str(existing.get("response_source") or "checkpoint-reuse"),
            )
            return "ok", output, ""

    request = read_json_object(checkpoint_dir / "request.json")
    if not request:
        return "safe-to-submit", "", "synthesis was never prepared"
    if request.get("input_sha256") != input_sha256:
        return "failed", "", "synthesis checkpoint input fingerprint does not match the current role reports"
    try:
        recorded_project = Path(str(request.get("project_dir") or "")).expanduser().resolve()
        recorded_checkpoint = Path(str(request.get("checkpoint_dir") or "")).expanduser().resolve()
        state_path = Path(str(request.get("state_path") or "")).expanduser().resolve()
        journal_path = Path(str(request.get("journal_path") or "")).expanduser().resolve()
    except OSError:
        return "failed", "", "synthesis checkpoint contains an invalid local path"
    if (
        recorded_project != args.project_dir
        or recorded_checkpoint != checkpoint_dir
        or state_path != checkpoint_dir / "conversation.json"
        or journal_path != checkpoint_dir / "turn-journal.json"
    ):
        return "failed", "", "synthesis checkpoint paths do not match the current private run directory"
    journal = read_json_object(journal_path)
    saved_state = read_json_object(state_path)
    conversation = saved_state.get("conversation") if isinstance(saved_state.get("conversation"), dict) else {}
    conversation_id = conversation.get("conversation_id") if isinstance(conversation, dict) else None
    if not conversation_id and not advisor_agent.journal_proves_submission(journal):
        return "safe-to-submit", "", "synthesis journal proves no submission began"

    prompt = str(request.get("prompt") or "")
    marker = str(request.get("marker") or "")
    project_id = str(request.get("chatgpt_project_id") or saved_state.get("chatgpt_project_id") or "")
    if not prompt or not marker or marker not in prompt:
        return "failed", "", "synthesis checkpoint lacks its exact recovery marker"
    if not project_id:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import advisor  # noqa: PLC0415

        project_id = str(advisor.chatgpt_project_id(allow_create=False) or "")
    if not project_id:
        return "pending", "", "submitted synthesis has no bound ChatGPT Project id"

    if isinstance(conversation_id, str) and conversation_id:
        remote_data, error = advisor_agent.fetch_conversation_by_id(conversation_id, args.timeout)
    else:
        remote_data, conversation_id, error = advisor_agent.discover_exact_remote_conversation(
            project_id,
            prompt,
            args.timeout,
        )
    if error or not remote_data or not conversation_id:
        return "pending", "", error or "submitted synthesis is not yet discoverable"
    output = advisor_agent.final_text_from_conversation_data(remote_data, prompt)
    if not output:
        return "pending", "", "submitted synthesis has not produced a finished final response"
    if marker not in output:
        return "failed", "", "recovered synthesis final omitted its completion marker"
    output = advisor_agent.strip_completion_marker(output, marker)
    advisor_agent.persist_recovered_conversation(
        state_path=state_path,
        project_id=project_id,
        conversation_id=conversation_id,
        data=remote_data,
    )
    publish_synthesis_checkpoint(
        output_dir,
        checkpoint_dir,
        input_sha256,
        output,
        source="interrupted-run-remote-recovery",
    )
    return "ok", output, ""


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
    parser.add_argument(
        "--resume-run",
        type=Path,
        help="Resume an interrupted conclave. Submitted roles are recovered with GET-only requests.",
    )
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


def execute_conclave_run(
    args: argparse.Namespace,
    *,
    task: str,
    roles: list[str],
    specs: dict[str, dict[str, str]],
    output_dir: Path,
    manifest_path: Path,
    resume: bool,
) -> int:
    started = time.monotonic()
    if args.dry_run:
        role_results = [
            AgentRoleResult(role, True, f"[dry-run] Would launch repo-aware {role}.", 0.0, {"status": "dry-run"})
            for role in roles
        ]
    else:
        role_results = (
            resume_roles(args, roles, task, specs, manifest_path)
            if resume
            else run_roles(args, roles, task, specs, manifest_path)
        )

    failed_roles = [result.role for result in role_results if not result.ok]
    synthesis_ok = True
    synthesis = "Synthesis skipped."
    synthesis_error = ""
    can_synthesize = not failed_roles or args.allow_partial
    successful_roles = [result for result in role_results if result.ok]
    if not args.no_synthesis and not args.dry_run and can_synthesize and successful_roles:
        recovery_status = "safe-to-submit"
        if resume:
            recovery_status, synthesis, synthesis_error = recover_synthesis(
                args,
                output_dir,
                task,
                successful_roles,
            )
        if recovery_status == "safe-to-submit":
            synthesis_ok, synthesis, synthesis_error = run_synthesis(
                args,
                task,
                successful_roles,
                output_dir,
            )
        elif recovery_status == "ok":
            synthesis_ok = True
        else:
            synthesis_ok = False
            synthesis = "Synthesis recovery is pending or failed closed."
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
    with concurrency.InterProcessLock(manifest_path.with_suffix(".lock"), timeout=30.0):
        manifest = read_json_object(manifest_path)
        manifest.update(
            {
                "status": payload["status"],
                "updated_utc": utc_now(),
                "completed_utc": utc_now(),
                "report_path": str(report_path),
                "metadata_path": str(metadata_path),
            }
        )
        safety.atomic_write_json(manifest_path, manifest)
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


def main() -> int:
    configure_stdio()
    args = parse_args()
    resume_manifest: dict[str, Any] = {}
    if args.resume_run is not None:
        resume_manifest = read_json_object(args.resume_run.expanduser().resolve() / "manifest.json")
        if not resume_manifest:
            print("The requested conclave run has no readable manifest.json checkpoint.", file=sys.stderr)
            return 2
        recorded_project = Path(str(resume_manifest.get("project_dir") or "")).expanduser().resolve()
        if args.project_dir is not None and args.project_dir.expanduser().resolve() != recorded_project:
            print("--project-dir does not match the interrupted conclave manifest.", file=sys.stderr)
            return 2
        args.project_dir = recorded_project
        try:
            output_dir = validated_resume_dir(recorded_project, args.resume_run)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        task = str(resume_manifest.get("task") or "").strip()
        args.mode = str(resume_manifest.get("mode") or args.mode)
        args.provider = str(resume_manifest.get("provider") or args.provider)
        args.base_url = str(resume_manifest.get("base_url") or args.base_url)
        args.model = str(resume_manifest.get("model") or "") or None
        args.thinking_effort = str(resume_manifest.get("thinking_effort") or "") or None
        args.timeout = int(resume_manifest.get("request_timeout_seconds") or 0)
        args.queue_timeout = float(resume_manifest.get("queue_timeout_seconds") or 0)
        args.max_output_tokens = int(resume_manifest.get("max_output_tokens") or args.max_output_tokens)
        args.parallel = bool(resume_manifest.get("parallel", args.parallel))
        args.max_workers = int(resume_manifest.get("max_workers") or args.max_workers)
        args.allow_partial = bool(resume_manifest.get("allow_partial", args.allow_partial))
        args.no_synthesis = bool(resume_manifest.get("no_synthesis", args.no_synthesis))
        args.live_activity = bool(resume_manifest.get("live_activity", args.live_activity))
    else:
        args.project_dir = resolve_project(args.project_dir)
        task = safety.redact_sensitive_text(
            safety.sanitize_text(args.prompt if args.prompt is not None else sys.stdin.read())
        ).strip()
    if not task:
        print("Provide --prompt or pipe task text on stdin.", file=sys.stderr)
        return 2
    if resume_manifest:
        raw_roles = resume_manifest.get("roles")
        roles = [str(role) for role in raw_roles] if isinstance(raw_roles, list) else []
        if not roles or any(role not in conclave.ROLE_PROMPTS or role == "synthesizer" for role in roles):
            print("The interrupted conclave manifest has invalid specialist roles.", file=sys.stderr)
            return 2
    else:
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
    if args.provider != "openai-compatible" or not concurrency.local_http_url(args.base_url):
        print(
            "Repo-aware agent conclave requires a loopback OpenAI-compatible endpoint; "
            "refusing to send repository-derived prompts to a remote API.",
            file=sys.stderr,
        )
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

    if resume_manifest:
        raw_specs = resume_manifest.get("role_runs")
        if not isinstance(raw_specs, dict):
            print("The interrupted conclave manifest has no role run checkpoints.", file=sys.stderr)
            return 2
        specs = {}
        for role in roles:
            value = raw_specs.get(role)
            if not isinstance(value, dict):
                print(f"The interrupted conclave manifest is missing role {role!r}.", file=sys.stderr)
                return 2
            role_dir = Path(str(value.get("run_dir") or "")).expanduser().resolve()
            try:
                role_dir.relative_to(output_dir)
            except ValueError:
                print(f"The interrupted conclave role {role!r} escaped its run directory.", file=sys.stderr)
                return 2
            token = str(value.get("recovery_token") or "")
            if not token:
                print(f"The interrupted conclave role {role!r} has no recovery token.", file=sys.stderr)
                return 2
            specs[role] = {**value, "run_dir": str(role_dir), "recovery_token": token}
        manifest_path = output_dir / "manifest.json"
    else:
        output_dir = run_dir(args.project_dir, args.mode)
        specs = role_run_specs(output_dir, roles)
        manifest_path = write_initial_manifest(
            output_dir,
            args=args,
            roles=roles,
            task=task,
            specs=specs,
        )
    try:
        with concurrency.InterProcessLock(
            output_dir / "run.lock",
            timeout=1.0,
            wait_message="Another process is already reconciling this advisor conclave run.",
        ):
            return execute_conclave_run(
                args,
                task=task,
                roles=roles,
                specs=specs,
                output_dir=output_dir,
                manifest_path=manifest_path,
                resume=bool(resume_manifest),
            )
    except RuntimeError as exc:
        print(f"Advisor conclave run lock failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
