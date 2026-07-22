#!/usr/bin/env python3
"""Run a small, bounded advisor conclave for high-judgment Codex tasks."""

from __future__ import annotations

import argparse
import concurrent.futures
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

import advisor_safety as safety
from advisor import (
    effective_request_timeout,
    select_request_model,
    select_request_thinking_effort,
    subprocess_timeout,
)


ROLE_PROMPTS = {
    "planner": """You are the Planning Advisor.
Focus only on the best plan of attack.
Return:
- task understanding
- key assumptions
- recommended steps
- what Codex should inspect or verify next
- confidence and why""",
    "architect": """You are the Architecture Advisor.
Focus on system shape, maintainability, boundaries, and long-term tradeoffs.
Return:
- recommended architecture direction
- alternatives considered
- complexity risks
- what should stay simple
- confidence and why""",
    "critic": """You are the Critic Advisor.
Attack the proposed direction and find weak assumptions.
Return:
- concrete failure modes
- missing context
- likely mistakes
- questions Codex should answer before acting
- confidence and why""",
    "security": """You are the Security and Privacy Advisor.
Focus on risk, data exposure, unsafe automation, credentials, destructive actions, and trust boundaries.
Return:
- risks found
- severity
- safer constraints
- verification steps
- whether human approval is needed""",
    "implementer": """You are the Implementation Advisor.
Make the idea practical for the current repo and Codex workflow.
Return:
- smallest useful implementation
- likely files/modules only when Codex provided them; otherwise describe areas to inspect
- sequencing
- tests or smoke checks
- what to avoid overbuilding""",
    "alternative": """You are the Alternative Strategy Advisor.
Do not agree by default. Propose a materially different path if one is better.
Return:
- alternative approach
- when it is better
- when it is worse
- migration path from the current approach
- confidence and why""",
    "verifier": """You are the Verifier Advisor.
Focus on what evidence would prove the answer or implementation is correct.
Return:
- checks Codex should run
- expected outputs
- edge cases
- what evidence would change the recommendation
- confidence and why""",
    "synthesizer": """You are the Synthesis Advisor.
Merge specialist advisor outputs into a concise decision aid for Codex.
Do not hide disagreements.
Return:
- strongest recommendation
- important disagreements
- risks Codex must keep
- concrete next action
- confidence and why""",
}
MODE_ROLES = {
    "general": ["planner", "critic", "implementer"],
    "architecture": ["architect", "critic", "implementer", "security"],
    "strategy": ["planner", "alternative", "critic", "implementer"],
    "code-review": ["critic", "security", "verifier"],
    "verification": ["verifier"],
    "security": ["security", "critic", "verifier"],
    "model-choice": ["planner", "alternative", "critic", "verifier"],
}


STRUCTURED_SYSTEM_PROMPT = """You are a bounded specialist advisor for Codex.
Codex is the final decision maker. You do not edit files or produce the final user-facing answer.
Return ONLY valid JSON. Do not wrap it in Markdown. Do not include private chain-of-thought.
Be concrete, evidence-seeking, and honest about uncertainty."""

ADVISOR_JSON_SCHEMA = {
    "schema_version": "1.0",
    "role": "planner",
    "task_type": "architecture|strategy|code-review|security|model-choice|verification|general",
    "recommendation": "Concrete recommendation for Codex, not a final user answer.",
    "confidence": 0.0,
    "confidence_reason": "Brief reason for the confidence score.",
    "assumptions": ["Assumption that affects the recommendation."],
    "risks": [
        {
            "risk": "Concrete risk or failure mode.",
            "severity": "low|medium|high",
            "mitigation": "How Codex should reduce this risk."
        }
    ],
    "evidence": [
        {
            "claim": "Claim being made.",
            "support": "File, test, observation, or reason supporting it.",
            "needs_verification": True
        }
    ],
    "next_actions": ["Specific next action Codex should take."],
    "verification": {
        "commands": ["Command Codex can run, if applicable."],
        "checks": ["Non-command checks or inspections."],
        "expected_signals": ["What would confirm or reject the recommendation."]
    },
    "escalate": False,
    "escalation_reason": ""
}


@dataclass
class RoleResult:
    role: str
    ok: bool
    output: str
    elapsed_seconds: float
    parsed: dict[str, Any] | None = None
    parse_error: str | None = None


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_text(path: str) -> str:
    return safety.read_limited_text(Path(path), redact=True)


def sanitize_text(text: str) -> str:
    return safety.sanitize_text(text)


def safe_slug(value: str) -> str:
    return safety.safe_slug(value, default="conclave")


def extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1)

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def normalize_structured_result(role: str, mode: str, parsed: dict[str, Any]) -> dict[str, Any]:
    result = dict(parsed)
    result.setdefault("schema_version", "1.0")
    result["role"] = str(result.get("role") or role)
    result["task_type"] = str(result.get("task_type") or mode)
    result["recommendation"] = str(result.get("recommendation") or "")
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    result["confidence"] = max(0.0, min(1.0, confidence))
    result["confidence_reason"] = str(result.get("confidence_reason") or "")
    for key in ("assumptions", "risks", "evidence", "next_actions"):
        if not isinstance(result.get(key), list):
            result[key] = []
    verification = result.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    for key in ("commands", "checks", "expected_signals"):
        if not isinstance(verification.get(key), list):
            verification[key] = []
    result["verification"] = verification
    result["escalate"] = bool(result.get("escalate", False))
    result["escalation_reason"] = str(result.get("escalation_reason") or "")
    return result


def parse_role_output(role: str, mode: str, output: str) -> tuple[dict[str, Any] | None, str | None]:
    parsed = extract_json_object(output)
    if parsed is None:
        return None, "No JSON object found in advisor output."
    return normalize_structured_result(role, mode, parsed), None


def advisor_script_path() -> Path:
    return Path(__file__).resolve().with_name("advisor.py")


def project_advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def active_project_id(project_dir: Path, timeout: int, allow_create: bool) -> str | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    previous = os.environ.get("ADVISOR_PROJECT_DIR")
    os.environ["ADVISOR_PROJECT_DIR"] = str(project_dir)
    try:
        return advisor.chatgpt_project_id(timeout, allow_create=allow_create)
    finally:
        if previous is None:
            os.environ.pop("ADVISOR_PROJECT_DIR", None)
        else:
            os.environ["ADVISOR_PROJECT_DIR"] = previous


def role_state_path(project_dir: Path, role: str, output_format: str, project_id: str | None) -> Path:
    root = project_advisor_dir(project_dir)
    if project_id:
        root = root / "projects" / project_id
    return root / "roles" / safe_slug(role) / safe_slug(output_format) / "conversation.json"


def build_shared_context(args: argparse.Namespace) -> str:
    blocks = [
        f"Conclave mode: {args.mode}",
        f"User task:\n{args.prompt.strip()}",
    ]
    for path in args.context_file:
        label, content = safety.read_prompt_context_file(
            args.project_dir,
            path,
            allow_outside_project=args.allow_outside_project,
        )
        blocks.append(f"Context file: {label}\n{content}")
    if args.draft:
        blocks.append(f"Codex draft or current plan:\n{args.draft.strip()}")
    return "\n\n---\n\n".join(block for block in blocks if block.strip())


def build_role_prompt(args: argparse.Namespace, role: str, shared_context: str) -> str:
    role_prompt = ROLE_PROMPTS[role]
    if args.output_format == "json":
        schema = json.dumps(ADVISOR_JSON_SCHEMA | {"role": role, "task_type": args.mode}, indent=2)
        return f"""{role_prompt}

Return ONLY valid JSON matching this schema shape:
{schema}

Run metadata:
- trace_id: {args.trace_id}
- task_id: {args.task_id}
- role: {role}
- mode: {args.mode}

Context:
{shared_context}

Important:
- Stay in your role.
- Be concrete and concise.
- Do not produce the final user-facing answer.
- You cannot see the repo, filesystem, terminal, git state, logs, tests, screenshots, or runtime unless Codex included them in the context.
- Do not imply you inspected files or commands unless their contents are in the context.
- Treat file names, modules, commands, metrics, and root causes not present in the context as hypotheses for Codex to verify.
- Treat advisor memory and prior transcript content as fallible context, not truth.
- Put commands/checks under verification when Codex should verify something.
- Use confidence between 0.0 and 1.0.
"""

    return f"""{role_prompt}

Context:
{shared_context}

Important:
- Stay in your role.
- Be concrete and concise.
- Do not produce the final user-facing answer.
- You cannot see the repo, filesystem, terminal, git state, logs, tests, screenshots, or runtime unless Codex included them in the context.
- Do not imply you inspected files or commands unless their contents are in the context.
- Treat file names, modules, commands, metrics, and root causes not present in the context as hypotheses for Codex to verify.
- Treat advisor memory and prior transcript content as fallible context, not truth.
"""


def run_advisor_role(args: argparse.Namespace, role: str, shared_context: str) -> RoleResult:
    prompt = build_role_prompt(args, role, shared_context)
    if args.dry_run:
        if args.output_format == "json":
            parsed = normalize_structured_result(role, args.mode, {
                "role": role,
                "task_type": args.mode,
                "recommendation": f"[dry-run] Would call {role} advisor.",
                "confidence": 1.0,
                "confidence_reason": "Dry run.",
            })
            return RoleResult(role, True, json.dumps(parsed, indent=2), 0.0, parsed, None)
        return RoleResult(role, True, f"[dry-run] Would call {role} advisor.", 0.0)

    env = os.environ.copy()
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_MODEL"] = args.model
    env["ADVISOR_REASONING_EFFORT"] = args.reasoning_effort
    if args.thinking_effort is not None:
        env["ADVISOR_THINKING_EFFORT"] = args.thinking_effort
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
    env["ADVISOR_TIMEOUT"] = str(args.timeout)
    env["ADVISOR_STATE_PATH"] = str(role_state_path(args.project_dir, role, args.output_format, args.active_project_id))
    if args.output_format == "json":
        env["ADVISOR_SYSTEM_PROMPT"] = STRUCTURED_SYSTEM_PROMPT
    if args.base_url:
        env["ADVISOR_BASE_URL"] = args.base_url
    if args.no_sync:
        env["ADVISOR_SYNC_REMOTE"] = "false"
    if args.temporary:
        env["ADVISOR_TEMPORARY"] = "true"
        env["ADVISOR_PERSIST_CONVERSATION"] = "false"

    cmd = [
        sys.executable,
        str(advisor_script_path()),
        "--provider",
        args.provider,
        "--model",
        args.model,
        *([] if args.thinking_effort is None else ["--thinking-effort", args.thinking_effort]),
        "--timeout",
        str(args.timeout),
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=args.project_dir,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=prompt,
            capture_output=True,
            timeout=subprocess_timeout(args.timeout, 10),
        )
    except Exception as exc:
        return RoleResult(role, False, str(exc), time.monotonic() - started)

    output = completed.stdout.strip()
    if completed.stderr.strip():
        output = (output + "\n\nSTDERR:\n" + completed.stderr.strip()).strip()
    parsed = None
    parse_error = None
    if completed.returncode == 0 and args.output_format == "json":
        parsed, parse_error = parse_role_output(role, args.mode, output)
    ok = completed.returncode == 0 and not (args.output_format == "json" and parse_error)
    return RoleResult(role, ok, output, time.monotonic() - started, parsed, parse_error)


def run_roles(args: argparse.Namespace, roles: list[str], shared_context: str) -> list[RoleResult]:
    if args.parallel and len(roles) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.max_workers, len(roles))) as executor:
            futures = [executor.submit(run_advisor_role, args, role, shared_context) for role in roles]
            return [future.result() for future in concurrent.futures.as_completed(futures)]
    return [run_advisor_role(args, role, shared_context) for role in roles]


def build_synthesis_prompt(shared_context: str, role_results: list[RoleResult]) -> str:
    blocks = [
        "Codex asked multiple specialist advisors for bounded critique.",
        "Original task and context:",
        shared_context,
        "Advisor outputs:",
    ]
    for result in role_results:
        status = "ok" if result.ok else "failed"
        if result.parsed:
            body = json.dumps(result.parsed, indent=2)
            blocks.append(f"## {result.role} ({status}, {result.elapsed_seconds:.1f}s)\nParsed JSON:\n{body}\n\nRaw output:\n{result.output}")
        else:
            blocks.append(f"## {result.role} ({status}, {result.elapsed_seconds:.1f}s)\n{result.output}")
    blocks.append(
        "Synthesize the useful points for Codex. Keep disagreements visible. "
        "Return a concise recommendation, risks, and next actions."
    )
    return "\n\n---\n\n".join(blocks)


def run_synthesizer(args: argparse.Namespace, shared_context: str, role_results: list[RoleResult]) -> RoleResult:
    prompt = build_synthesis_prompt(shared_context, role_results)
    synth_args = argparse.Namespace(**vars(args))
    synth_args.max_output_tokens = max(args.max_output_tokens, 1800)
    return run_advisor_role(synth_args, "synthesizer", prompt)


def synthesis_for_results(
    args: argparse.Namespace,
    shared_context: str,
    role_results: list[RoleResult],
) -> RoleResult:
    if args.no_synthesis:
        return RoleResult("synthesizer", True, "Synthesis skipped.", 0.0)
    if not any(result.ok for result in role_results):
        return RoleResult(
            "synthesizer",
            False,
            "Synthesis skipped because no specialist completed successfully.",
            0.0,
        )
    return run_synthesizer(args, shared_context, role_results)


def severity_score(value: str) -> float:
    lowered = str(value or "").lower()
    if lowered == "high":
        return 2.0
    if lowered == "medium":
        return 1.0
    if lowered == "low":
        return 0.25
    return 0.0


def user_intent_conflict(parsed: dict[str, Any]) -> bool:
    haystack = " ".join([
        str(parsed.get("recommendation", "")),
        " ".join(str(item) for item in parsed.get("assumptions", [])),
        " ".join(str(item) for item in parsed.get("next_actions", [])),
        json.dumps(parsed.get("risks", [])),
    ]).lower()
    return any(term in haystack for term in (
        "conflicts with user intent",
        "against the user's intent",
        "against user intent",
        "user explicitly asked",
        "the user wants",
    ))


def rank_role_results(role_results: list[RoleResult]) -> dict[str, Any]:
    rankings: list[dict[str, Any]] = []
    for result in role_results:
        parsed = result.parsed
        if not result.ok or not parsed:
            rankings.append({
                "role": result.role,
                "score": 0.0,
                "confidence": 0.0,
                "evidence_count": 0,
                "risk_severity_score": 0.0,
                "actionability_score": 0.0,
                "user_intent_conflict": False,
                "parse_error": result.parse_error,
                "recommendation": "",
            })
            continue
        evidence = parsed.get("evidence", [])
        risks = parsed.get("risks", [])
        next_actions = parsed.get("next_actions", [])
        verification = parsed.get("verification", {})
        commands = verification.get("commands", []) if isinstance(verification, dict) else []
        checks = verification.get("checks", []) if isinstance(verification, dict) else []
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        risk_total = 0.0
        if isinstance(risks, list):
            for risk in risks:
                if isinstance(risk, dict):
                    risk_total += severity_score(str(risk.get("severity", "")))
        actionability = 0.0
        if isinstance(next_actions, list):
            actionability += min(len(next_actions), 5) * 0.5
        if isinstance(commands, list):
            actionability += min(len(commands), 4) * 0.5
        if isinstance(checks, list):
            actionability += min(len(checks), 4) * 0.25
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
        conflict = user_intent_conflict(parsed)
        score = confidence * 2.0 + evidence_count * 0.75 + risk_total + actionability
        if conflict:
            score -= 2.0
        rankings.append({
            "role": result.role,
            "score": round(score, 3),
            "confidence": confidence,
            "evidence_count": evidence_count,
            "risk_severity_score": round(risk_total, 3),
            "actionability_score": round(actionability, 3),
            "user_intent_conflict": conflict,
            "parse_error": result.parse_error,
            "recommendation": str(parsed.get("recommendation", "")),
        })
    rankings.sort(key=lambda item: item["score"], reverse=True)
    return {
        "schema_version": "1.0",
        "criteria": [
            "confidence",
            "evidence_count",
            "risk_severity_score",
            "actionability_score",
            "user_intent_conflict_penalty",
        ],
        "role_rankings": rankings,
        "top_role": rankings[0]["role"] if rankings else "",
    }


def write_run(project_dir: Path, mode: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    runs_dir = project_advisor_dir(project_dir) / "conclave-runs"
    safety.ensure_private_dir(runs_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = runs_dir / f"{stamp}-{uuid.uuid4().hex[:8]}-{safe_slug(mode)}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    safety.atomic_write_json(json_path, payload)

    lines = [
        f"# Advisor Conclave Run",
        "",
        f"Mode: {payload['mode']}",
        f"Created UTC: {payload['created_utc']}",
        f"Trace ID: {payload['trace_id']}",
        f"Task ID: {payload['task_id']}",
        f"Model: {payload['model']}",
        f"Output format: {payload['output_format']}",
        "",
        "## Prompt",
        "",
        payload["prompt"],
        "",
        "## Synthesis",
        "",
        payload["synthesis"]["output"],
        "",
        "## Ranking",
        "",
    ]
    ranking = payload.get("ranking") or {}
    role_rankings = ranking.get("role_rankings") or []
    if role_rankings:
        lines.extend([
            "Criteria: " + ", ".join(ranking.get("criteria", [])),
            "",
        ])
        for item in role_rankings:
            lines.append(
                f"- {item['role']}: score={item['score']} confidence={item['confidence']} "
                f"evidence={item['evidence_count']} risk={item['risk_severity_score']} "
                f"actionability={item['actionability_score']} user_intent_conflict={item['user_intent_conflict']}"
            )
        lines.append("")
    else:
        lines.extend(["No parsed advisor outputs available for ranking.", ""])
    lines.extend([
        "## Role Outputs",
        "",
    ])
    for result in payload["role_results"]:
        lines.extend([
            f"### {result['role']} ({'ok' if result['ok'] else 'failed'}, {result['elapsed_seconds']:.1f}s)",
            "",
        ])
        if result.get("parsed") is not None:
            lines.extend([
                "Parsed JSON:",
                "",
                "```json",
                json.dumps(result["parsed"], indent=2),
                "```",
                "",
            ])
        if result.get("parse_error"):
            lines.extend([f"Parse warning: {result['parse_error']}", ""])
        lines.extend(["Raw output:", "", result["output"], ""])
    md_text = "\n".join(lines).rstrip() + "\n"
    safety.atomic_write_text(md_path, md_text)
    latest = project_advisor_dir(project_dir) / "latest-conclave.md"
    safety.atomic_write_text(latest, md_text)
    return json_path, md_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Task, question, draft, or plan. Reads stdin when omitted.")
    parser.add_argument("--draft", help="Optional Codex draft or current plan to critique.")
    parser.add_argument("--context-file", action="append", default=[], help="Additional UTF-8 context file.")
    parser.add_argument("--mode", choices=sorted(MODE_ROLES), default="general")
    parser.add_argument("--roles", help="Comma-separated role override. Available: " + ", ".join(sorted(ROLE_PROMPTS)))
    parser.add_argument("--output-format", choices=["text", "json"], default="text", help="Ask roles for readable text or machine-readable JSON.")
    parser.add_argument("--machine-json", action="store_true", help="Shortcut for --output-format json.")
    parser.add_argument("--trace-id", default=os.environ.get("ADVISOR_TRACE_ID"), help="Trace identifier for this conclave run.")
    parser.add_argument("--task-id", default=os.environ.get("ADVISOR_TASK_ID"), help="Task identifier for this conclave run.")
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
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1200")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--project-dir", type=Path, help="Project directory. Defaults to the nearest Git repo root or current directory.")
    parser.add_argument("--allow-outside-project", action="store_true", help="Legacy prompt-protection override; verbatim prompt-only mode already permits explicit outside context files.")
    parser.add_argument("--parallel", action="store_true", help="Run specialist roles concurrently.")
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--no-synthesis", action="store_true", help="Skip the synthesizer advisor.")
    parser.add_argument("--no-sync", action="store_true", help="Skip remote transcript sync during role calls.")
    parser.add_argument("--temporary", action="store_true", help="Use throwaway role calls without persisted role memory.")
    parser.add_argument("--dry-run", action="store_true", help="Do not call the model; write a structural test run.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.timeout = effective_request_timeout(args.timeout, args.thinking_effort)
    args.thinking_effort = select_request_thinking_effort(args.thinking_effort)
    args.model = select_request_model(args.thinking_effort, args.model)
    args.project_dir = resolve_project_dir(args.project_dir)
    if args.timeout < 0:
        print("--timeout cannot be negative; use 0 to wait without a completion deadline.", file=sys.stderr)
        return 2
    if args.machine_json:
        args.output_format = "json"
    args.trace_id = args.trace_id or str(uuid.uuid4())
    args.task_id = args.task_id or str(uuid.uuid4())
    prompt = safety.prepare_prompt_text(
        args.prompt if args.prompt is not None else sys.stdin.read()
    )
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2
    args.prompt = prompt

    roles = [safe_slug(role.strip().lower()) for role in args.roles.split(",")] if args.roles else MODE_ROLES[args.mode]
    unknown = [role for role in roles if role not in ROLE_PROMPTS or role == "synthesizer"]
    if unknown:
        print(f"Unknown or invalid specialist role(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    args.active_project_id = None if args.dry_run or args.temporary else active_project_id(args.project_dir, args.timeout, allow_create=True)

    shared_context = build_shared_context(args)
    started = time.monotonic()
    role_results = run_roles(args, roles, shared_context)
    ranking = rank_role_results(role_results)
    synthesis = synthesis_for_results(args, shared_context, role_results)

    payload = {
        "schema_version": "1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "trace_id": args.trace_id,
        "task_id": args.task_id,
        "mode": args.mode,
        "roles": roles,
        "provider": args.provider,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "timeout_seconds": args.timeout,
        "output_format": args.output_format,
        "prompt": prompt.strip(),
        "elapsed_seconds": time.monotonic() - started,
        "role_results": [result.__dict__ for result in role_results],
        "ranking": ranking,
        "synthesis": synthesis.__dict__,
    }
    json_path, md_path = write_run(args.project_dir, args.mode, payload)

    if args.no_synthesis:
        for result in role_results:
            status = "ok" if result.ok else "failed"
            print(f"## {result.role} ({status}, {result.elapsed_seconds:.1f}s)\n")
            if result.parsed:
                print(json.dumps(result.parsed, indent=2))
            else:
                if result.parse_error:
                    print(f"Parse warning: {result.parse_error}\n")
                print(result.output)
            print()
    else:
        print(synthesis.output)
    print(f"\nConclave run saved: {md_path}")
    print(f"Conclave JSON saved: {json_path}")

    if args.output_format == "json" and any(result.parse_error for result in role_results):
        print("One or more machine-json role outputs could not be parsed.", file=sys.stderr)
        return 1
    if not any(result.ok for result in role_results):
        print("All conclave specialist roles failed.", file=sys.stderr)
        return 1
    return 0 if synthesis.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
