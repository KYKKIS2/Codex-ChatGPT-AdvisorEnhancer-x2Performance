#!/usr/bin/env python3
"""Route Codex tasks to the right advisor path."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECURITY_TERMS = {
    "auth", "authentication", "authorization", "cookie", "token", "secret", "password",
    "credential", "api key", "private key", "oauth", "jwt", "session", "csrf", "xss",
    "sql injection", "sandbox", "permission", "privacy", "security", "exploit",
}

ARCHITECTURE_TERMS = {
    "architecture", "architect", "design", "refactor", "system", "scalability",
    "maintainability", "tradeoff", "trade-off", "roadmap", "strategy", "approach",
    "direction", "workflow", "pipeline", "orchestration", "integration",
}

MODEL_CHOICE_TERMS = {
    "model", "train", "training", "rl", "reinforcement", "classifier", "regression",
    "embedding", "fine tune", "finetune", "llm", "agent", "framework", "tool",
    "library", "choose", "selection", "which should", "what should i use",
}

DEBUG_TERMS = {
    "error", "traceback", "exception", "failed", "failing", "failure", "bug",
    "regression", "test failed", "pytest", "stack trace", "doesn't work", "not working",
}

ROUTINE_TERMS = {
    "fix typo", "rename", "format", "lint", "small change", "update copy",
    "simple", "quick", "minor", "one-line", "one line",
}


@dataclass
class RouteDecision:
    route: str
    command_kind: str
    mode: str | None
    roles: list[str]
    machine_json: bool
    confidence: float
    reasons: list[str]
    skip_reason: str = ""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def sanitize_text(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")


def contains_any(text: str, terms: set[str]) -> list[str]:
    lowered = text.lower()
    return sorted(term for term in terms if term in lowered)


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def route_log_dir(project_dir: Path) -> Path:
    return advisor_dir(project_dir) / "routes"


def script_path(name: str) -> Path:
    return Path(__file__).resolve().with_name(name)


def resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def command_preview(decision: RouteDecision, args: argparse.Namespace) -> list[str]:
    if decision.route == "no-advisor":
        return []
    if decision.command_kind == "advisor":
        return [
            sys.executable,
            str(script_path("advisor.py")),
            "--provider", args.provider,
            "--model", args.model,
            "--timeout", str(args.timeout),
        ]
    if decision.command_kind == "verifier-loop":
        command = [
            sys.executable,
            str(script_path("verifier_loop.py")),
            "--provider", args.provider,
            "--model", args.model,
            "--timeout", str(args.timeout),
            "--project-dir", str(args.project_dir),
        ]
        if args.no_sync:
            command.append("--no-sync")
        if args.draft:
            command.extend(["--draft", args.draft])
        return command
    command = [
        sys.executable,
        str(script_path("conclave.py")),
        "--provider", args.provider,
        "--model", args.model,
        "--timeout", str(args.timeout),
        "--mode", decision.mode or "general",
    ]
    if decision.roles:
        command.extend(["--roles", ",".join(decision.roles)])
    if decision.machine_json:
        command.append("--machine-json")
    if args.no_sync:
        command.append("--no-sync")
    single_role_route = decision.command_kind == "conclave" and len(decision.roles) == 1
    if args.no_synthesis or single_role_route:
        command.append("--no-synthesis")
    return command


def route_task(args: argparse.Namespace, prompt: str) -> RouteDecision:
    text = " ".join([
        prompt,
        args.draft or "",
        args.error_output or "",
        " ".join(args.changed_file),
        " ".join(args.tag),
    ]).strip()
    words = re.findall(r"\w+", text)
    reasons: list[str] = []
    security_hits = contains_any(text, SECURITY_TERMS)

    if security_hits and not args.allow_sensitive_advisor:
        reasons.append("security/privacy terms: " + ", ".join(security_hits[:6]))
        return RouteDecision(
            "no-advisor",
            "none",
            None,
            [],
            False,
            0.9,
            reasons,
            "Sensitive/security-related task. Handle locally or rerun with --allow-sensitive-advisor after redacting context.",
        )

    explicit = args.force_route
    if explicit:
        reasons.append(f"forced route: {explicit}")
        return forced_decision(explicit, reasons)

    if args.before_final:
        reasons.append("before-final review requested")
        return RouteDecision("single-advisor", "conclave", "general", ["critic"], False, 0.95, reasons)

    if args.machine_verify:
        reasons.append("machine-json verifier requested")
        return RouteDecision("machine-json-verifier", "verifier-loop", "verification", ["verifier"], True, 0.96, reasons)

    if args.failed_tests or args.error_output:
        reasons.append("failed tests or error output present")
        return RouteDecision("verifier", "verifier-loop", "verification", ["verifier"], False, 0.88, reasons)

    if security_hits:
        reasons.append("security/privacy terms allowed by --allow-sensitive-advisor: " + ", ".join(security_hits[:6]))
        return RouteDecision("conclave", "conclave", "security", ["security", "critic", "verifier"], False, 0.9, reasons)

    model_hits = contains_any(text, MODEL_CHOICE_TERMS)
    if model_hits:
        reasons.append("model/tool choice terms: " + ", ".join(model_hits[:6]))
        return RouteDecision("conclave", "conclave", "model-choice", ["planner", "alternative", "critic"], False, 0.84, reasons)

    architecture_hits = contains_any(text, ARCHITECTURE_TERMS)
    if architecture_hits:
        reasons.append("architecture/strategy terms: " + ", ".join(architecture_hits[:6]))
        route = "conclave" if len(words) > 80 or args.high_impact else "single-advisor"
        if route == "conclave":
            return RouteDecision(route, "conclave", "architecture", ["architect", "critic", "implementer"], False, 0.82, reasons)
        return RouteDecision(route, "advisor", None, [], False, 0.74, reasons)

    debug_hits = contains_any(text, DEBUG_TERMS)
    if debug_hits and (args.high_impact or args.changed_file):
        reasons.append("debug/failure terms with non-trivial scope: " + ", ".join(debug_hits[:6]))
        return RouteDecision("verifier", "verifier-loop", "verification", ["verifier"], False, 0.76, reasons)

    routine_hits = contains_any(text, ROUTINE_TERMS)
    if routine_hits and len(words) < 80 and not args.high_impact:
        reasons.append("routine low-risk task terms: " + ", ".join(routine_hits[:6]))
        return RouteDecision("no-advisor", "none", None, [], False, 0.86, reasons, "Routine low-risk task.")

    if args.high_impact or len(words) > 140:
        if args.high_impact:
            reasons.append("high-impact flag set")
        if len(words) > 140:
            reasons.append(f"long/ambiguous task ({len(words)} words)")
        return RouteDecision("single-advisor", "advisor", None, [], False, 0.7, reasons)

    reasons.append("no advisor trigger matched")
    return RouteDecision("no-advisor", "none", None, [], False, 0.68, reasons, "No complexity, risk, or verification trigger matched.")


def forced_decision(route: str, reasons: list[str]) -> RouteDecision:
    if route == "no-advisor":
        return RouteDecision(route, "none", None, [], False, 1.0, reasons, "Forced no-advisor route.")
    if route == "single-advisor":
        return RouteDecision(route, "advisor", None, [], False, 1.0, reasons)
    if route == "conclave":
        return RouteDecision(route, "conclave", "strategy", ["planner", "critic", "implementer"], False, 1.0, reasons)
    if route == "verifier":
        return RouteDecision(route, "verifier-loop", "verification", ["verifier"], False, 1.0, reasons)
    if route == "machine-json-verifier":
        return RouteDecision(route, "verifier-loop", "verification", ["verifier"], True, 1.0, reasons)
    raise ValueError(f"Unknown forced route: {route}")


def build_payload(args: argparse.Namespace, prompt: str, decision: RouteDecision) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "trace_id": args.trace_id,
        "task_id": args.task_id,
        "prompt": prompt.strip(),
        "draft_present": bool(args.draft),
        "changed_files": args.changed_file,
        "tags": args.tag,
        "failed_tests": args.failed_tests,
        "high_impact": args.high_impact,
        "before_final": args.before_final,
        "context_files": args.context_file,
        "auto_context_pack": not args.no_context_pack,
        "allow_sensitive_advisor": args.allow_sensitive_advisor,
        "route": decision.route,
        "command_kind": decision.command_kind,
        "mode": decision.mode,
        "roles": decision.roles,
        "machine_json": decision.machine_json,
        "confidence": decision.confidence,
        "reasons": decision.reasons,
        "skip_reason": decision.skip_reason,
        "command_preview": command_preview(decision, args),
    }


def write_route(project_dir: Path, payload: dict[str, Any]) -> Path:
    out_dir = route_log_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}-{payload['route']}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest = advisor_dir(project_dir) / "latest-route.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def build_context_pack(args: argparse.Namespace, prompt: str) -> Path | None:
    command = [
        sys.executable,
        str(script_path("context_pack.py")),
        "--json",
        "--project-dir", str(args.project_dir),
        "--trace-id", args.trace_id,
        "--task-id", args.task_id,
        "--prompt", prompt,
    ]
    if args.draft:
        command.extend(["--draft", args.draft])
    if args.error_output:
        command.extend(["--failure", args.error_output])
    for path in args.changed_file:
        command.extend(["--file", path])
    for path in args.context_file:
        command.extend(["--context-file", path])
    for tag in args.tag:
        command.extend(["--constraint", f"Task tag: {tag}"])
    completed = subprocess.run(
        command,
        cwd=args.project_dir,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        print("Context pack generation failed; continuing without it.", file=sys.stderr)
        if completed.stderr.strip():
            print(completed.stderr.strip(), file=sys.stderr)
        return None
    json_path = Path(completed.stdout.strip().splitlines()[-1])
    md_path = json_path.with_suffix(".md")
    return md_path if md_path.exists() else None


def execute_route(args: argparse.Namespace, prompt: str, decision: RouteDecision) -> int:
    if decision.route == "no-advisor":
        print("Router selected no-advisor.")
        print(decision.skip_reason)
        return 0
    command = command_preview(decision, args)
    context_files = list(args.context_file)
    if not args.no_context_pack:
        pack_path = build_context_pack(args, prompt)
        if pack_path:
            context_files.append(str(pack_path))
    if decision.command_kind in {"conclave", "verifier-loop"}:
        for path in context_files:
            command.extend(["--context-file", path])
    env = os.environ.copy()
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_BASE_URL"] = args.base_url
    env["ADVISOR_MODEL"] = args.model
    env["ADVISOR_REASONING_EFFORT"] = args.reasoning_effort
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
    if args.draft and decision.command_kind != "verifier-loop":
        prompt = f"{prompt.strip()}\n\n--- Codex draft/current plan ---\n{args.draft.strip()}"
    if decision.command_kind == "advisor" and context_files:
        blocks = []
        for path in context_files:
            try:
                blocks.append(f"Context file: {path}\n{Path(path).read_text(encoding='utf-8')}")
            except OSError as exc:
                blocks.append(f"Context file unavailable: {path}\n{exc}")
        prompt = f"{prompt.strip()}\n\n--- Advisor context pack ---\n" + "\n\n---\n\n".join(blocks)
    if args.error_output:
        prompt = f"{prompt.strip()}\n\n--- Error output / failed evidence ---\n{args.error_output.strip()}"
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=args.project_dir,
        env=env,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=args.timeout + 30,
    )
    elapsed = time.monotonic() - started
    print(f"\nRouter execution finished in {elapsed:.1f}s with exit code {completed.returncode}.")
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Task, question, draft, or plan. Reads stdin when omitted.")
    parser.add_argument("--draft", help="Codex draft/current plan for critique or verification.")
    parser.add_argument("--draft-file", help="Read Codex draft/current plan from a file.")
    parser.add_argument("--error-output", help="Failed test, traceback, or command output.")
    parser.add_argument("--error-file", help="Read failed test, traceback, or command output from a file.")
    parser.add_argument("--changed-file", action="append", default=[], help="Relevant changed file path.")
    parser.add_argument("--context-file", action="append", default=[], help="Extra context file to pass to advisor routes.")
    parser.add_argument("--tag", action="append", default=[], help="Extra task tag or signal.")
    parser.add_argument("--failed-tests", action="store_true", help="Signal that tests failed or need verification.")
    parser.add_argument("--high-impact", action="store_true", help="Signal that a wrong answer has meaningful cost.")
    parser.add_argument("--before-final", action="store_true", help="Route to critic-only before final answer.")
    parser.add_argument("--machine-verify", action="store_true", help="Route to machine-json verifier.")
    parser.add_argument("--force-route", choices=["no-advisor", "single-advisor", "conclave", "verifier", "machine-json-verifier"])
    parser.add_argument("--execute", action="store_true", help="Execute the selected advisor route.")
    parser.add_argument("--json", action="store_true", help="Print only JSON route decision.")
    parser.add_argument("--provider", choices=["openai", "openai-compatible"], default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"))
    parser.add_argument("--base-url", default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL", "gpt-5-5-thinking"))
    parser.add_argument("--reasoning-effort", default=os.environ.get("ADVISOR_REASONING_EFFORT", "high"))
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1200")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--project-dir", type=Path, help="Project directory. Defaults to the nearest Git repo root or current directory.")
    parser.add_argument("--allow-sensitive-advisor", action="store_true", help="Allow advisor routing for security/privacy/auth/token tasks after caller redaction.")
    parser.add_argument("--trace-id", default=os.environ.get("ADVISOR_TRACE_ID"))
    parser.add_argument("--task-id", default=os.environ.get("ADVISOR_TASK_ID"))
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--no-synthesis", action="store_true")
    parser.add_argument("--no-context-pack", action="store_true", help="Do not auto-build a compact context pack during --execute.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.project_dir = resolve_project_dir(args.project_dir)
    args.trace_id = args.trace_id or str(uuid.uuid4())
    args.task_id = args.task_id or str(uuid.uuid4())
    prompt = sanitize_text(args.prompt if args.prompt is not None else sys.stdin.read())
    if args.draft_file:
        args.draft = sanitize_text(Path(args.draft_file).read_text(encoding="utf-8"))
    if args.error_file:
        args.error_output = sanitize_text(Path(args.error_file).read_text(encoding="utf-8"))
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2

    decision = route_task(args, prompt)
    payload = build_payload(args, prompt, decision)
    path = write_route(args.project_dir, payload)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Route: {decision.route}")
        if decision.mode:
            print(f"Mode: {decision.mode}")
        if decision.roles:
            print(f"Roles: {', '.join(decision.roles)}")
        print(f"Confidence: {decision.confidence:.2f}")
        print("Reasons:")
        for reason in decision.reasons:
            print(f"- {reason}")
        if decision.skip_reason:
            print(f"Skip reason: {decision.skip_reason}")
        if payload["command_preview"]:
            print("Command preview:")
            print(" ".join(payload["command_preview"]))
        print(f"Route saved: {path}")

    if args.execute:
        return execute_route(args, prompt, decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
