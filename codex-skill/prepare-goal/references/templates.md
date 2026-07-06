# Templates

Use these structures when the repository does not already define its own PRD, plan, or playbook format.

## PRD

```md
# PRD: <Title>

## Problem

<What is wrong, missing, risky, or inefficient?>

## Goal

<What should be true when the work is complete?>

## Non-Goals

- <What this task intentionally will not change>

## Users / Stakeholders

- <Primary users or maintainers affected>

## Requirements

- <Functional or operational requirement>

## Acceptance Criteria

- <Observable success condition>

## Constraints

- <Public contracts, data contracts, auth, payments, privacy, env vars, routes, schemas, or compatibility constraints>

## Open Questions

- <Only unresolved product or safety questions>
```

## ExecPlan

```md
# ExecPlan: <Title>

This plan follows `AGENTS.md` and `docs/PLAYBOOK.md`.

## Purpose / Big Picture

<Technical end state and why it matters.>

## Progress

- [ ] <Milestone or concrete step>

## Surprises & Discoveries

Document unexpected findings here.

## Decision Log

- Decision: <Choice>
  Rationale: <Why this is the safest useful option>
  Date/Author: <YYYY-MM-DD> / Codex

## Context and Orientation

Relevant files:
- `<path>`

Current friction:
- <Observed issue or risk>

## Plan of Work

<Implementation approach in concrete, reviewable steps.>

## Milestones

### Milestone 1: <Name>

Work:
- <Step>

Validation:
- `<command>`

## Risks and Rollback

- Risk: <Risk>
  Mitigation: <Mitigation or rollback>

## Outcomes & Retrospective

Fill this in after implementation.
```

## Playbook

```md
# PLAYBOOK.md

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
```

## Goal Template

```text
/goal Implement `<plan-path>` using `AGENTS.md`, `docs/PLAYBOOK.md`, and `<prd-path>`. Preserve <critical public contracts and non-goals>. Work autonomously through the plan milestones, update the plan as discoveries occur, add or update tests, run <validation commands>, and stop only when the acceptance criteria are met or a blocker under AGENTS.md rules requires user input.
```
