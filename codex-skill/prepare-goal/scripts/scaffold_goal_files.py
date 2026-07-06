#!/usr/bin/env python3
"""Create conservative PRD/ExecPlan/playbook scaffolds for goal preparation."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path


PLAYBOOK_TEMPLATE = """# PLAYBOOK.md

## How We Refactor

1. Preserve external behavior first.
2. Find a vertical slice.
3. Add characterization tests if behavior is unclear.
4. Move logic behind focused modules or stable contracts.
5. Keep routes, controllers, and entrypoints thin.
6. Run tests after each milestone.
7. Record decisions in the plan.

## How We Debug

1. Reproduce the failure.
2. Find the smallest failing test or command.
3. Explain the suspected cause.
4. Patch narrowly.
5. Prove the fix.
6. Add regression coverage.

## How We Choose Architecture

Prefer boring, explicit, typed modules. Use dependency injection only where it improves testability. Avoid generic registries unless there are several real cases. Preserve public contracts during refactors.
"""


PRD_TEMPLATE = """# PRD: {title}

## Problem

<Describe the problem, missing capability, risk, or maintenance issue.>

## Goal

<Describe what should be true when the work is complete.>

## Non-Goals

- <List behavior, surfaces, contracts, or implementation areas intentionally out of scope.>

## Users / Stakeholders

- <Identify affected users, admins, maintainers, or systems.>

## Requirements

- <Add functional, operational, security, privacy, or compatibility requirements.>

## Acceptance Criteria

- <Add observable checks that prove the task is complete.>

## Constraints

- <List public APIs, route URLs, response shapes, database contracts, env vars, auth, payments, PII, or deployment constraints that must stay intact.>

## Open Questions

- <Add only unresolved product or safety questions.>
"""


PLAN_TEMPLATE = """# ExecPlan: {title}

This plan follows `AGENTS.md` and `docs/PLAYBOOK.md`.

## Purpose / Big Picture

<Describe the technical end state and why it matters.>

## Progress

- [ ] Inspect current repo context and relevant files.
- [ ] Confirm constraints from the PRD.
- [ ] Implement the first safe vertical slice.
- [ ] Add or update tests.
- [ ] Run validation and review the diff.

## Surprises & Discoveries

Document unexpected findings here.

## Decision Log

- Decision: Preserve existing public behavior unless the PRD explicitly says otherwise.
  Rationale: Keeps the work reviewable and reduces regression risk.
  Date/Author: {today} / Codex

## Context and Orientation

Relevant files:
- <Add exact paths after inspecting the repo.>

Current friction:
- <Summarize observed issue or risk.>

## Plan of Work

<Write the implementation approach as concrete, reviewable steps.>

## Milestones

### Milestone 1: Characterize Current Behavior

Work:
- <Inspect current implementation and identify contracts to preserve.>

Validation:
- <Add the narrowest useful command.>

### Milestone 2: Implement First Slice

Work:
- <Make the smallest coherent change.>

Validation:
- <Add tests or checks for the changed path.>

### Milestone 3: Final Validation

Work:
- <Review diff, docs, and plan updates.>

Validation:
- <Add broader checks when practical.>

## Risks and Rollback

- Risk: <Risk>
  Mitigation: <Mitigation>

## Outcomes & Retrospective

Fill this in after implementation.
"""


GOAL_TEMPLATE = """# Goal Templates

```text
/goal Implement `docs/plans/{slug}.md` using `AGENTS.md`, `docs/PLAYBOOK.md`, and `docs/prds/{slug}.md`. Preserve the PRD constraints and existing public contracts unless explicitly changed in the PRD. Work autonomously through the plan milestones, update the plan as discoveries occur, add or update tests, run the plan validation commands, and stop only when the acceptance criteria are met or a blocker under AGENTS.md rules requires user input.
```
"""


def write_if_missing(path: Path, content: str) -> str:
    if path.exists():
        return f"exists: {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"created: {path}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create goal-preparation scaffolds without overwriting existing files.")
    parser.add_argument("--root", default=".", help="Repository root or target directory.")
    parser.add_argument("--slug", required=True, help="Lowercase hyphenated task slug.")
    parser.add_argument("--title", required=True, help="Human-readable task title.")
    parser.add_argument("--playbook", action="store_true", help="Create docs/PLAYBOOK.md if missing.")
    parser.add_argument("--goal-templates", action="store_true", help="Create prompts/goal-templates.md if missing.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    slug = args.slug.strip().lower()
    title = args.title.strip()
    if not slug or any(ch for ch in slug if not (ch.islower() or ch.isdigit() or ch == "-")):
        raise SystemExit("--slug must contain only lowercase letters, digits, and hyphens")
    if not title:
        raise SystemExit("--title is required")

    messages: list[str] = []
    if args.playbook:
        messages.append(write_if_missing(root / "docs" / "PLAYBOOK.md", PLAYBOOK_TEMPLATE))
    messages.append(write_if_missing(root / "docs" / "prds" / f"{slug}.md", PRD_TEMPLATE.format(title=title)))
    messages.append(
        write_if_missing(
            root / "docs" / "plans" / f"{slug}.md",
            PLAN_TEMPLATE.format(title=title, today=date.today().isoformat()),
        )
    )
    if args.goal_templates:
        messages.append(write_if_missing(root / "prompts" / "goal-templates.md", GOAL_TEMPLATE.format(slug=slug)))

    print("\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
