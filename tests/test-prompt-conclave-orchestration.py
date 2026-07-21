#!/usr/bin/env python3
"""Offline regressions for prompt-only Pro Extended orchestration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "codex-skill" / "external-advisor" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import advisor  # noqa: E402
import conclave  # noqa: E402
import router  # noqa: E402


def assert_timeout_policy() -> None:
    if advisor.effective_request_timeout(300, "pro-extended", timeout_explicit=False) != 0:
        raise AssertionError("implicit Pro Extended timeout was not made unlimited")
    if advisor.effective_request_timeout(300, "pro-extended", timeout_explicit=True) != 300:
        raise AssertionError("explicit Pro Extended operator deadline was not preserved")
    if advisor.effective_request_timeout(300, "max", timeout_explicit=False) != 300:
        raise AssertionError("normal advisor timeout changed unexpectedly")
    if advisor.subprocess_timeout(0, 30) is not None:
        raise AssertionError("unlimited advisor timeout became a subprocess deadline")
    if advisor.subprocess_timeout(7, 10) != 17:
        raise AssertionError("bounded advisor subprocess grace calculation changed")


def base_args(project: Path, timeout: int) -> argparse.Namespace:
    return argparse.Namespace(
        provider="openai-compatible",
        model="gpt-5-6-pro",
        reasoning_effort="high",
        thinking_effort="pro-extended",
        max_output_tokens=1200,
        timeout=timeout,
        project_dir=project,
        active_project_id=None,
        output_format="text",
        mode="model-choice",
        trace_id="offline-timeout-test",
        task_id="offline-timeout-test",
        base_url="http://127.0.0.1:8080/v1",
        no_sync=False,
        temporary=False,
        dry_run=False,
        no_synthesis=False,
    )


def assert_child_timeout_propagation() -> None:
    seen: list[float | None] = []
    original_run = conclave.subprocess.run

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess([], 0, stdout="offline result", stderr="")

    try:
        conclave.subprocess.run = fake_run
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            unlimited = conclave.run_advisor_role(base_args(project, 0), "planner", "context")
            bounded = conclave.run_advisor_role(base_args(project, 7), "planner", "context")
    finally:
        conclave.subprocess.run = original_run
    if not unlimited.ok or not bounded.ok or seen != [None, 17]:
        raise AssertionError(f"prompt-only role timeout propagation was wrong: {seen!r}")


def assert_router_timeout_propagation() -> None:
    args = argparse.Namespace(
        timeout=0,
        agent_timeout=0,
        agent_queue_timeout=0,
        agent_max_workers=5,
        no_synthesis=False,
    )
    decision = router.RouteDecision(
        "conclave",
        "conclave",
        "model-choice",
        ["planner", "alternative", "critic"],
        False,
        1.0,
        [],
    )
    if router.route_execution_timeout(args, decision) is not None:
        raise AssertionError("router imposed an outer deadline on an unlimited conclave")
    args.timeout = 12
    if router.route_execution_timeout(args, decision) != 138:
        raise AssertionError("router bounded conclave timeout calculation changed unexpectedly")


def assert_failed_roles_skip_synthesis() -> None:
    args = argparse.Namespace(no_synthesis=False)
    failures = [
        conclave.RoleResult("planner", False, "failed", 310.0),
        conclave.RoleResult("critic", False, "failed", 310.0),
    ]
    original = conclave.run_synthesizer

    def forbidden(*_args: object, **_kwargs: object) -> conclave.RoleResult:
        raise AssertionError("synthesizer ran despite zero successful specialists")

    try:
        conclave.run_synthesizer = forbidden
        result = conclave.synthesis_for_results(args, "context", failures)
    finally:
        conclave.run_synthesizer = original
    if result.ok or "no specialist completed" not in result.output.lower():
        raise AssertionError("all-failed conclave did not record a skipped synthesis")


def assert_conclave_cli_timeout_resolution() -> None:
    env = os.environ.copy()
    env["ADVISOR_THINKING_EFFORT"] = "pro-extended"
    env.pop("ADVISOR_TIMEOUT", None)
    for explicit, expected in (([], 0), (["--timeout", "7"], 7)):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "conclave.py"),
                    "--project-dir",
                    str(project),
                    "--dry-run",
                    "--no-synthesis",
                    "--roles",
                    "planner",
                    "--prompt",
                    "Offline timeout policy test.",
                    *explicit,
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                raise AssertionError(
                    "offline conclave timeout probe failed: "
                    f"{completed.stdout!r} {completed.stderr!r}"
                )
            paths = list((project / ".codex-advisor" / "conclave-runs").glob("*.json"))
            if len(paths) != 1:
                raise AssertionError(f"offline conclave wrote unexpected artifacts: {paths!r}")
            payload = json.loads(paths[0].read_text(encoding="utf-8"))
            if payload.get("timeout_seconds") != expected:
                raise AssertionError(
                    "conclave CLI resolved the wrong timeout: "
                    f"{payload.get('timeout_seconds')!r} != {expected!r}"
                )


def main() -> None:
    assert_timeout_policy()
    assert_child_timeout_propagation()
    assert_router_timeout_propagation()
    assert_failed_roles_skip_synthesis()
    assert_conclave_cli_timeout_resolution()
    print("Prompt-only conclave orchestration tests passed.")


if __name__ == "__main__":
    main()
