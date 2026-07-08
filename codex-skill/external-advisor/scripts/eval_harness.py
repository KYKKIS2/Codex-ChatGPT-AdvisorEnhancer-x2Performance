#!/usr/bin/env python3
"""Run local advisor benchmark experiments."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import advisor_safety as safety
from advisor import select_request_model, select_request_thinking_effort


STRATEGIES = ("codex-only", "single-advisor", "conclave", "critic-verifier")

BENCHMARKS = {
    "architecture": [
        "Choose an architecture for project-scoped advisor memory.",
        "Design a migration path from one chat memory to role-specific memories.",
        "Decide whether advisor context should be pulled or pushed into calls.",
        "Choose boundaries between router, context pack, verifier, and memory modules.",
        "Plan how to make advisor calls reliable when the browser-backed endpoint is fragile.",
        "Decide how to isolate machine JSON conversations from readable online chats.",
        "Design a compact memory layout for long-lived project decisions.",
        "Choose whether verification should be automatic or manually triggered.",
        "Plan a safe command execution policy for advisor-suggested checks.",
        "Design an official OpenAI-native version of this advisor workflow.",
    ],
    "code-review": [
        "Review a patch that changes advisor state paths.",
        "Review a patch that adds command execution from advisor suggestions.",
        "Review a patch that writes context packs with git diffs.",
        "Review a patch that adds transcript sync before advisor calls.",
        "Review a patch that changes model slug defaults.",
        "Review a patch that updates setup scripts for Windows and Ubuntu.",
        "Review a patch that stores accepted and rejected advice.",
        "Review a patch that parses machine JSON from model output.",
        "Review a patch that adds parallel conclave calls.",
        "Review a patch that updates README safety guidance.",
    ],
    "debugging": [
        "Router sends failed-test tasks to the wrong advisor mode.",
        "Critique output is JSON even though readable text was requested.",
        "Context pack generation fails outside a git repository.",
        "Verifier loop skips a safe pytest command.",
        "Memory summary does not show superseded decisions.",
        "Setup installs the skill but Codex cannot find start-g4f.ps1.",
        "Conclave validation passes even when parsed JSON is missing.",
        "Transcript sync writes stale local state after online chat edits.",
        "PowerShell test script treats $Args as a reserved variable.",
        "The local g4f server fails because port 8080 is already in use.",
    ],
    "model-choice": [
        "Choose default model slug between gpt-5-5 and gpt-5-5-pro.",
        "Decide whether model-choice questions need planner, alternative, critic, and verifier roles.",
        "Choose when to use machine JSON instead of readable advisor text.",
        "Decide whether Pro should be used for every advisor call.",
        "Choose a strategy for comparing model advice quality over time.",
        "Decide whether RL is useful for a trading research bot.",
        "Choose between classical ML and reinforcement learning for early token launch ranking.",
        "Decide whether to fine-tune or use prompt/context improvements first.",
        "Choose how to evaluate advisor latency versus answer quality.",
        "Decide whether to run multiple advisors serially or in parallel.",
    ],
}


@dataclass
class EvalResult:
    category: str
    task_index: int
    task: str
    strategy: str
    status: str
    elapsed_seconds: float
    exit_code: int | None
    output_chars: int
    tests_passed: bool | None
    quality_score: float | None
    notes: str


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def evaluations_dir(project_dir: Path) -> Path:
    return advisor_dir(project_dir) / "evaluations"


def script_path(name: str) -> Path:
    return Path(__file__).resolve().with_name(name)


def select_tasks(limit_per_category: int | None) -> list[tuple[str, int, str]]:
    tasks: list[tuple[str, int, str]] = []
    for category, prompts in BENCHMARKS.items():
        selected = prompts[:limit_per_category] if limit_per_category else prompts
        for index, prompt in enumerate(selected, start=1):
            tasks.append((category, index, prompt))
    return tasks


def mode_for_category(category: str) -> str:
    if category == "architecture":
        return "architecture"
    if category == "code-review":
        return "code-review"
    if category == "model-choice":
        return "model-choice"
    return "verification"


def run_command(args: argparse.Namespace, command: list[str], prompt: str) -> tuple[str, int | None, float, str]:
    env = os.environ.copy()
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_BASE_URL"] = args.base_url
    env["ADVISOR_MODEL"] = args.model
    env["ADVISOR_REASONING_EFFORT"] = args.reasoning_effort
    if args.thinking_effort is not None:
        env["ADVISOR_THINKING_EFFORT"] = args.thinking_effort
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
    started = time.monotonic()
    try:
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
        return "completed", completed.returncode, time.monotonic() - started, output
    except subprocess.TimeoutExpired as exc:
        output = (safety.sanitize_text(exc.stdout) + "\n" + safety.sanitize_text(exc.stderr)).strip()
        return "timeout", None, time.monotonic() - started, output
    except Exception as exc:
        return "error", None, time.monotonic() - started, str(exc)


def evaluate_strategy(args: argparse.Namespace, category: str, index: int, task: str, strategy: str) -> EvalResult:
    if args.dry_run:
        return EvalResult(category, index, task, strategy, "dry-run", 0.0, 0, 0, None, None, "Dry run; no model call.")
    if strategy == "codex-only":
        return EvalResult(
            category,
            index,
            task,
            strategy,
            "manual-baseline",
            0.0,
            None,
            0,
            None,
            None,
            "Codex-only quality must be filled from a real Codex answer outside this script.",
        )
    if strategy == "single-advisor":
        command = [
            sys.executable,
            str(script_path("advisor.py")),
            "--provider", args.provider,
            "--model", args.model,
            "--timeout", str(args.timeout),
        ]
        status, exit_code, elapsed, output = run_command(args, command, task)
    elif strategy == "conclave":
        command = [
            sys.executable,
            str(script_path("conclave.py")),
            "--provider", args.provider,
            "--model", args.model,
            "--timeout", str(args.timeout),
            "--mode", mode_for_category(category),
            "--machine-json",
            "--no-sync",
        ]
        status, exit_code, elapsed, output = run_command(args, command, task)
    else:
        draft = f"Candidate Codex answer for benchmark task: {task}\nThis is a placeholder draft for critic/verifier evaluation."
        command = [
            sys.executable,
            str(script_path("verifier_loop.py")),
            "--provider", args.provider,
            "--model", args.model,
            "--timeout", str(args.timeout),
            "--no-sync",
            "--draft", draft,
            "--command", "git status --short",
        ]
        status, exit_code, elapsed, output = run_command(args, command, task)
    return EvalResult(category, index, task, strategy, status, elapsed, exit_code, len(output), None, None, "")


def write_report(project_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = evaluations_dir(project_dir)
    safety.ensure_private_dir(out_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    unique = uuid.uuid4().hex[:8]
    json_path = out_dir / f"{stamp}-{unique}-evaluation.json"
    md_path = out_dir / f"{stamp}-{unique}-evaluation.md"
    safety.atomic_write_json(json_path, payload)
    lines = [
        "# Advisor Evaluation Run",
        "",
        f"Created UTC: {payload['created_utc']}",
        f"Run ID: {payload['run_id']}",
        f"Dry run: {payload['dry_run']}",
        f"Strategies: {', '.join(payload['strategies'])}",
        "",
        "## Summary",
        "",
    ]
    for item in payload["summary"]:
        lines.append(
            f"- {item['strategy']}: tasks={item['tasks']} completed={item['completed']} "
            f"avg_latency={item['average_elapsed_seconds']}"
        )
    lines.extend(["", "## Results", ""])
    for result in payload["results"]:
        lines.append(
            f"- {result['category']} #{result['task_index']} [{result['strategy']}]: "
            f"{result['status']} exit={result['exit_code']} latency={result['elapsed_seconds']:.1f}s "
            f"output_chars={result['output_chars']}"
        )
    md_text = "\n".join(lines).rstrip() + "\n"
    safety.atomic_write_text(md_path, md_text)
    latest_json = advisor_dir(project_dir) / "latest-evaluation.json"
    latest_md = advisor_dir(project_dir) / "latest-evaluation.md"
    safety.atomic_write_json(latest_json, payload)
    safety.atomic_write_text(latest_md, md_text)
    return json_path, md_path


def summarize(results: list[EvalResult]) -> list[dict[str, Any]]:
    summary = []
    for strategy in STRATEGIES:
        subset = [result for result in results if result.strategy == strategy]
        if not subset:
            continue
        elapsed = [result.elapsed_seconds for result in subset if result.elapsed_seconds is not None]
        summary.append({
            "strategy": strategy,
            "tasks": len(subset),
            "completed": sum(1 for result in subset if result.status in {"completed", "dry-run", "manual-baseline"}),
            "average_elapsed_seconds": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
            "tests_passed_count": sum(1 for result in subset if result.tests_passed is True),
            "quality_score_average": None,
        })
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=[*STRATEGIES, "all"], default="all")
    parser.add_argument("--limit-per-category", type=int, help="Use fewer than the built-in 10 tasks per category.")
    parser.add_argument("--dry-run", action="store_true", help="Validate benchmark structure without model calls.")
    parser.add_argument("--provider", choices=["openai", "openai-compatible"], default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"))
    parser.add_argument("--base-url", default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL"))
    parser.add_argument("--reasoning-effort", default=os.environ.get("ADVISOR_REASONING_EFFORT", "high"))
    parser.add_argument(
        "--thinking-effort",
        default=(
            os.environ.get("ADVISOR_THINKING_EFFORT")
            or os.environ.get("ADVISOR_CHATGPT_THINKING_EFFORT")
            or os.environ.get("ADVISOR_INTELLIGENCE")
        ),
        help="ChatGPT web intelligence/thinking effort, e.g. high->extended, extra-high->max, pro-extended, or none.",
    )
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1000")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--project-dir", type=Path, help="Project directory. Defaults to the nearest Git repo root or current directory.")
    return parser.parse_args()


def resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.thinking_effort = select_request_thinking_effort(args.thinking_effort)
    args.model = select_request_model(args.thinking_effort, args.model)
    args.project_dir = resolve_project_dir(args.project_dir)
    strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    results: list[EvalResult] = []
    for category, index, task in select_tasks(args.limit_per_category):
        for strategy in strategies:
            results.append(evaluate_strategy(args, category, index, task, strategy))
    payload = {
        "schema_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": str(uuid.uuid4()),
        "dry_run": args.dry_run,
        "strategies": strategies,
        "tasks_per_category": args.limit_per_category or 10,
        "categories": sorted(BENCHMARKS),
        "summary": summarize(results),
        "results": [asdict(result) for result in results],
    }
    json_path, md_path = write_report(args.project_dir, payload)
    print(f"Evaluation saved: {md_path}")
    print(f"Evaluation JSON saved: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
