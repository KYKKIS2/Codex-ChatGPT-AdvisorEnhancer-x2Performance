#!/usr/bin/env python3
"""Manage searchable advisor memory, outcomes, and decision hygiene."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PROFILE = """# Project Profile

Purpose:
- Unknown yet.

Constraints:
- Keep advisor context compact.
- Treat advisor memory as fallible.

Current Direction:
- Unknown yet.
"""

DEFAULT_LESSONS = """# Advisor Lessons

Record lessons that should change future advisor use.
"""

DEFAULT_QUESTIONS = """# Open Questions

Track unresolved questions that future Codex sessions should remember.
"""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def age_days(value: str) -> float | None:
    created = parse_iso(value)
    if not created:
        return None
    return round((datetime.now(timezone.utc) - created).total_seconds() / 86400, 2)


def memory_path(project_dir: Path, name: str) -> Path:
    return advisor_dir(project_dir) / name


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".invalid")
        backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_markdown(path: Path, title: str, body: str, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    entry = "\n\n".join([
        f"## {title}",
        f"Created UTC: {now_iso()}",
        f"Source: {source}",
        "",
        body.strip(),
        "",
    ])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + entry.strip() + "\n")


def init_memory(project_dir: Path) -> None:
    root = advisor_dir(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    defaults = {
        "project-profile.md": DEFAULT_PROFILE,
        "advisor-lessons.md": DEFAULT_LESSONS,
        "open-questions.md": DEFAULT_QUESTIONS,
    }
    for name, content in defaults.items():
        path = root / name
        if not path.exists():
            path.write_text(content.rstrip() + "\n", encoding="utf-8")
    for name in ("decisions.json", "outcomes.json"):
        path = root / name
        if not path.exists():
            write_json(path, [])


def record_outcome(args: argparse.Namespace) -> dict[str, Any]:
    init_memory(args.project_dir)
    path = memory_path(args.project_dir, "outcomes.json")
    data = load_json(path, [])
    item = {
        "id": args.id or str(uuid.uuid4()),
        "created_utc": now_iso(),
        "task": args.task,
        "advisor_mode": args.advisor_mode,
        "accepted_advice": args.accepted_advice,
        "rejected_advice": args.rejected_advice,
        "outcome": args.outcome,
        "useful": parse_bool(args.useful) if args.useful is not None else None,
        "source": args.source,
        "confidence": clamp_confidence(args.confidence),
        "status": args.status,
        "notes": args.notes,
    }
    data.append(item)
    write_json(path, data)
    return item


def record_decision(args: argparse.Namespace) -> dict[str, Any]:
    init_memory(args.project_dir)
    path = memory_path(args.project_dir, "decisions.json")
    data = load_json(path, [])
    item = {
        "id": args.id or str(uuid.uuid4()),
        "created_utc": now_iso(),
        "decision": args.decision,
        "rationale": args.rationale,
        "source": args.source,
        "confidence": clamp_confidence(args.confidence),
        "status": args.status,
        "accepted": args.status == "accepted",
        "rejected": args.status == "rejected",
        "contradictions": args.contradiction,
        "supersedes": args.supersedes,
        "superseded_by": "",
        "tags": args.tag,
    }
    supersedes = set(args.supersedes)
    for existing in data:
        if existing.get("id") in supersedes:
            existing["status"] = "superseded"
            existing["superseded_by"] = item["id"]
    data.append(item)
    write_json(path, data)
    return item


def record_lesson(args: argparse.Namespace) -> dict[str, Any]:
    init_memory(args.project_dir)
    append_markdown(memory_path(args.project_dir, "advisor-lessons.md"), args.title or "Lesson", args.lesson, args.source)
    return {"lesson": args.lesson, "source": args.source}


def record_question(args: argparse.Namespace) -> dict[str, Any]:
    init_memory(args.project_dir)
    append_markdown(memory_path(args.project_dir, "open-questions.md"), args.title or "Question", args.question, args.source)
    return {"question": args.question, "source": args.source}


def clamp_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("Expected true/false.")


def summary(project_dir: Path) -> dict[str, Any]:
    init_memory(project_dir)
    decisions = load_json(memory_path(project_dir, "decisions.json"), [])
    outcomes = load_json(memory_path(project_dir, "outcomes.json"), [])
    for item in decisions:
        item["age_days"] = age_days(item.get("created_utc", ""))
    for item in outcomes:
        item["age_days"] = age_days(item.get("created_utc", ""))
    active_decisions = [item for item in decisions if item.get("status") in {"proposed", "accepted"}]
    stale_decisions = [
        item for item in active_decisions
        if item.get("age_days") is not None and item["age_days"] >= 30 and (item.get("confidence") or 0) < 0.7
    ]
    useful = sum(1 for item in outcomes if item.get("useful") is True)
    not_useful = sum(1 for item in outcomes if item.get("useful") is False)
    return {
        "schema_version": "1.0",
        "created_utc": now_iso(),
        "decision_count": len(decisions),
        "active_decision_count": len(active_decisions),
        "outcome_count": len(outcomes),
        "useful_outcomes": useful,
        "not_useful_outcomes": not_useful,
        "stale_low_confidence_decisions": stale_decisions,
        "recent_decisions": decisions[-10:],
        "recent_outcomes": outcomes[-10:],
    }


def write_summary(project_dir: Path, data: dict[str, Any]) -> Path:
    path = memory_path(project_dir, "memory-summary.md")
    lines = [
        "# Advisor Memory Summary",
        "",
        f"Created UTC: {data['created_utc']}",
        "",
        f"Decisions: {data['decision_count']}",
        f"Active decisions: {data['active_decision_count']}",
        f"Outcomes: {data['outcome_count']}",
        f"Useful outcomes: {data['useful_outcomes']}",
        f"Not useful outcomes: {data['not_useful_outcomes']}",
        "",
        "## Stale Low-Confidence Decisions",
        "",
    ]
    stale = data["stale_low_confidence_decisions"]
    if stale:
        for item in stale:
            lines.append(f"- `{item.get('id')}` age={item.get('age_days')}d confidence={item.get('confidence')}: {item.get('decision')}")
    else:
        lines.append("- None")
    lines.extend(["", "## Recent Decisions", ""])
    for item in data["recent_decisions"]:
        lines.append(f"- `{item.get('id')}` [{item.get('status')}] confidence={item.get('confidence')}: {item.get('decision')}")
    lines.extend(["", "## Recent Outcomes", ""])
    for item in data["recent_outcomes"]:
        lines.append(f"- `{item.get('id')}` useful={item.get('useful')} mode={item.get('advisor_mode')}: {item.get('outcome')}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    outcome = sub.add_parser("record-outcome")
    outcome.add_argument("--id")
    outcome.add_argument("--task", required=True)
    outcome.add_argument("--advisor-mode", required=True)
    outcome.add_argument("--accepted-advice", action="append", default=[])
    outcome.add_argument("--rejected-advice", action="append", default=[])
    outcome.add_argument("--outcome", required=True)
    outcome.add_argument("--useful")
    outcome.add_argument("--source", default="codex")
    outcome.add_argument("--confidence", type=float)
    outcome.add_argument("--status", choices=["accepted", "rejected", "mixed", "unknown"], default="unknown")
    outcome.add_argument("--notes", default="")

    decision = sub.add_parser("record-decision")
    decision.add_argument("--id")
    decision.add_argument("--decision", required=True)
    decision.add_argument("--rationale", default="")
    decision.add_argument("--source", default="codex")
    decision.add_argument("--confidence", type=float)
    decision.add_argument("--status", choices=["proposed", "accepted", "rejected", "superseded"], default="proposed")
    decision.add_argument("--contradiction", action="append", default=[])
    decision.add_argument("--supersedes", action="append", default=[])
    decision.add_argument("--tag", action="append", default=[])

    lesson = sub.add_parser("record-lesson")
    lesson.add_argument("--title")
    lesson.add_argument("--lesson", required=True)
    lesson.add_argument("--source", default="codex")

    question = sub.add_parser("record-question")
    question.add_argument("--title")
    question.add_argument("--question", required=True)
    question.add_argument("--source", default="codex")

    summary_cmd = sub.add_parser("summary")
    summary_cmd.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.project_dir = args.project_dir.resolve()
    if args.command == "init":
        init_memory(args.project_dir)
        print(f"Advisor memory initialized: {advisor_dir(args.project_dir)}")
        return 0
    if args.command == "record-outcome":
        item = record_outcome(args)
        print(json.dumps(item, indent=2))
        return 0
    if args.command == "record-decision":
        item = record_decision(args)
        print(json.dumps(item, indent=2))
        return 0
    if args.command == "record-lesson":
        print(json.dumps(record_lesson(args), indent=2))
        return 0
    if args.command == "record-question":
        print(json.dumps(record_question(args), indent=2))
        return 0
    if args.command == "summary":
        data = summary(args.project_dir)
        path = write_summary(args.project_dir, data)
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(f"Advisor memory summary saved: {path}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
