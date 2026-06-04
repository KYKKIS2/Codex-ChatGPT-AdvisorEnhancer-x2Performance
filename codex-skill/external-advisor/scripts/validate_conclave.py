#!/usr/bin/env python3
"""Validate saved advisor conclave run files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_TOP_LEVEL = {
    "schema_version",
    "created_utc",
    "trace_id",
    "task_id",
    "mode",
    "roles",
    "provider",
    "model",
    "output_format",
    "role_results",
    "synthesis",
}

REQUIRED_PARSED = {
    "schema_version",
    "role",
    "task_type",
    "recommendation",
    "confidence",
    "confidence_reason",
    "assumptions",
    "risks",
    "evidence",
    "next_actions",
    "verification",
    "escalate",
    "escalation_reason",
}


def latest_run(project_dir: Path) -> Path:
    runs_dir = project_dir / ".codex-advisor" / "conclave-runs"
    candidates = sorted(runs_dir.glob("*.json"))
    if not candidates:
        raise FileNotFoundError(f"No conclave run JSON files found in {runs_dir}")
    return candidates[-1]


def expect(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_parsed(role: str, parsed: dict[str, Any], errors: list[str]) -> None:
    missing = REQUIRED_PARSED - set(parsed)
    expect(not missing, f"{role}: parsed output missing keys: {', '.join(sorted(missing))}", errors)
    expect(isinstance(parsed.get("recommendation"), str), f"{role}: recommendation must be a string", errors)
    confidence = parsed.get("confidence")
    expect(isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0, f"{role}: confidence must be 0.0..1.0", errors)
    for key in ("assumptions", "risks", "evidence", "next_actions"):
        expect(isinstance(parsed.get(key), list), f"{role}: {key} must be a list", errors)
    verification = parsed.get("verification")
    expect(isinstance(verification, dict), f"{role}: verification must be an object", errors)
    if isinstance(verification, dict):
        for key in ("commands", "checks", "expected_signals"):
            expect(isinstance(verification.get(key), list), f"{role}: verification.{key} must be a list", errors)
    expect(isinstance(parsed.get("escalate"), bool), f"{role}: escalate must be a boolean", errors)


def validate_run(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    missing = REQUIRED_TOP_LEVEL - set(data)
    expect(not missing, f"run missing keys: {', '.join(sorted(missing))}", errors)
    role_results = data.get("role_results")
    expect(isinstance(role_results, list) and bool(role_results), "role_results must be a non-empty list", errors)
    if not isinstance(role_results, list):
        return errors
    for result in role_results:
        if not isinstance(result, dict):
            errors.append("role result must be an object")
            continue
        role = str(result.get("role") or "unknown")
        expect(isinstance(result.get("ok"), bool), f"{role}: ok must be a boolean", errors)
        expect(isinstance(result.get("output"), str), f"{role}: output must be a string", errors)
        if data.get("output_format") == "json" and result.get("ok"):
            parsed = result.get("parsed")
            expect(isinstance(parsed, dict), f"{role}: parsed JSON is required for successful json-format role output", errors)
            if isinstance(parsed, dict):
                validate_parsed(role, parsed, errors)
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json", nargs="?", help="Conclave run JSON file. Defaults to latest project run.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.run_json) if args.run_json else latest_run(args.project_dir)
    errors = validate_run(path)
    if errors:
        print(f"INVALID: {path}")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"VALID: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
