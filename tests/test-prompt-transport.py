#!/usr/bin/env python3
"""Offline regressions for prompt-only verbatim transport."""

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
import advisor_safety as safety  # noqa: E402
import conclave  # noqa: E402


MARKER = "api_key=VERBATIM_PROMPT_VALUE_123456789"


def without_prompt_protection() -> None:
    os.environ.pop("ADVISOR_PROMPT_PROTECTION", None)


def assert_shared_transport_policy(project: Path) -> None:
    context = project / ".env"
    context.write_text(MARKER + "\n", encoding="utf-8")

    without_prompt_protection()
    if safety.prepare_prompt_text(MARKER) != MARKER:
        raise AssertionError("default prompt-only transport changed deliberate text")
    _, content = safety.read_prompt_context_file(project, ".env")
    if content.strip() != MARKER:
        raise AssertionError("default prompt-only context transport changed explicit file content")
    outside = project.parent / "outside-prompt-context.txt"
    outside.write_text(MARKER + "\n", encoding="utf-8")
    _, outside_content = safety.read_prompt_context_file(project, str(outside))
    if outside_content.strip() != MARKER:
        raise AssertionError("default prompt-only transport rejected explicit outside context")

    previous_project = os.environ.get("ADVISOR_PROJECT_DIR")
    os.environ["ADVISOR_PROJECT_DIR"] = str(project)
    try:
        built = advisor.build_prompt("Inspect this value.", [".env"])
    finally:
        if previous_project is None:
            os.environ.pop("ADVISOR_PROJECT_DIR", None)
        else:
            os.environ["ADVISOR_PROJECT_DIR"] = previous_project
    if MARKER not in built:
        raise AssertionError("advisor.py did not preserve explicit prompt context")

    args = argparse.Namespace(
        mode="general",
        prompt="Inspect this value.",
        context_file=[".env"],
        project_dir=project,
        allow_outside_project=False,
        draft=MARKER,
    )
    shared = conclave.build_shared_context(args)
    if shared.count(MARKER) != 2:
        raise AssertionError("conclave.py did not preserve prompt-only draft and context")


def assert_context_pack_transport(project: Path) -> None:
    without_prompt_protection()
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "context_pack.py"),
            "--project-dir",
            str(project),
            "--no-git",
            "--no-memory",
            "--json",
            "--prompt",
            MARKER,
            "--context-file",
            ".env",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    payload = json.loads(Path(completed.stdout.strip()).read_text(encoding="utf-8"))
    if payload.get("task") != MARKER:
        raise AssertionError("context pack changed prompt-only task text")
    contexts = payload.get("extra_context_files") or []
    if len(contexts) != 1 or contexts[0].get("content", "").strip() != MARKER:
        raise AssertionError("context pack did not preserve explicit prompt-only context")


def assert_opt_in_legacy_protection(project: Path) -> None:
    os.environ["ADVISOR_PROMPT_PROTECTION"] = "true"
    try:
        protected = safety.prepare_prompt_text(MARKER)
        if protected == MARKER or "[REDACTED]" not in protected:
            raise AssertionError("legacy prompt protection did not redact an assignment")
        try:
            safety.read_prompt_context_file(project, ".env")
        except RuntimeError as exc:
            if "Refusing to include" not in str(exc):
                raise
        else:
            raise AssertionError("legacy prompt protection did not reject a protected context path")
        outside = project.parent / "outside-prompt-context.txt"
        try:
            safety.read_prompt_context_file(project, str(outside))
        except RuntimeError as exc:
            if "outside the project" not in str(exc):
                raise
        else:
            raise AssertionError("legacy prompt protection did not reject outside context")
    finally:
        without_prompt_protection()


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        project.mkdir()
        assert_shared_transport_policy(project)
        assert_context_pack_transport(project)
        assert_opt_in_legacy_protection(project)
    print("Prompt-only verbatim transport tests passed.")


if __name__ == "__main__":
    main()
