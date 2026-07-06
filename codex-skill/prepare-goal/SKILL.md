---
name: prepare-goal
description: Create the repo planning files needed before long-running Codex Goal mode and produce a ready-to-run `/goal` prompt. Use when the user asks to prepare a goal, convert an idea into AGENTS.md plus PRD plus ExecPlan/PLAN.md plus `/goal`, create planning scaffolds, ask the necessary clarifying questions before drafting plans or starting autonomous work, or turn fuzzy feature/refactor/debug/release work into durable Codex instructions.
---

# Prepare Goal

Use this skill to turn an undeveloped task into durable repo context and a concise `/goal` command. The output is not the implementation itself; it is the prepared instruction set Codex can execute in Goal mode. Do not draft PRDs, ExecPlans, or final `/goal` prompts until repo-discoverable facts have been inspected and any material clarifying questions have been answered or recorded as safe assumptions.

## Workflow

1. Inspect the repo context first.
   - Read the nearest `AGENTS.md` files, top-level README, package or project manifests, existing `docs/`, `.scratch/`, and any local planning conventions.
   - Search for existing PRDs, plans, playbooks, or issue files related to the requested task before creating new ones.
   - If the request is inside a git repo, check branch and dirty state before editing.

2. Classify the task enough to choose the planning shape and questions.
   - Use a PRD plus ExecPlan for features, refactors, migrations, multi-step debugging, releases, security hardening, or behavior-changing work.
   - Use only an ExecPlan for purely technical maintenance with no product-facing requirements.
   - Use a short final `/goal` without files only for clearly small tasks that do not need durable context.

3. Run the clarification gate before drafting planning files.
   - Use `references/question-checklist.md` before writing PRD, ExecPlan, or final `/goal` content.
   - Ask clarifying questions when the missing answer changes product behavior, public contracts, data safety, security posture, billing/payment behavior, scope, or acceptance criteria.
   - Prefer 3 to 7 focused questions. Do not ask the user to choose routine implementation details.
   - If a reasonable assumption is safe and reversible, state it in the PRD or plan and continue.
   - If a missing answer would make the PRD or plan misleading, unsafe, or impossible to validate, stop and ask before drafting those files.

4. Create or update durable files.
   - Prefer these repo-local paths unless existing repo conventions say otherwise:
     - `docs/PLAYBOOK.md` for durable human/team process, only if missing or explicitly requested.
     - `docs/prds/<slug>.md` for product intent and acceptance criteria.
     - `docs/plans/<slug>.md` for the execution plan.
     - `prompts/goal-templates.md` for reusable snippets, only if requested or useful for repeated work.
   - Do not overwrite existing files. Read them and merge narrowly.
   - Keep `AGENTS.md` concise. Add or modify it only when the user asks for durable agent behavior or when the task explicitly includes AGENTS guidance.
   - Use `scripts/scaffold_goal_files.py` when a clean skeleton is useful, then fill in the files from context.
   - Use `references/templates.md` for file structure.

5. Write the PRD and plan as execution inputs, not essays.
   - PRD: problem, goal, non-goals, users, requirements, acceptance criteria, constraints, open questions.
   - Plan: purpose, progress checklist, context, files to inspect or touch, milestones, validation commands, decision log, risks, outcomes.
   - Include exact preservation constraints such as route URLs, response shapes, env vars, migrations, permissions, and external contracts.
   - Include validation commands that match the repo's documented workflow.

6. Produce the final `/goal`.
   - Keep it under 4,000 characters.
   - Point to the plan, PRD, `AGENTS.md`, and `docs/PLAYBOOK.md` instead of embedding long instructions.
   - Include success checks, constraints to preserve, autonomous iteration policy, plan-update policy, and stop condition.
   - Do not start or set the goal unless the user explicitly asks and the current surface supports it.

## Final Output Shape

When the preparation is complete, respond with:

- Files created or updated.
- Important assumptions and unresolved questions, if any.
- The exact `/goal ...` command in a fenced `text` block.
- Validation performed, such as YAML validation, markdown checks, or repo status checks.

Use this goal pattern:

```text
/goal Implement `<plan-path>` using `AGENTS.md`, `<playbook-path>`, and `<prd-path>`. Preserve <critical constraints>. Work autonomously through the plan milestones, update the plan as discoveries occur, add or update tests, run <validation commands>, and stop only when the acceptance criteria are met or a blocker under AGENTS.md rules requires user input.
```

## Guardrails

- Do not use a giant `/goal` as the source of truth.
- Do not create a PRD alone for multi-hour work; pair it with an execution plan.
- Do not turn `prompts.txt` into the primary workflow source.
- Do not ask architecture multiple-choice questions unless the decision is product-facing, destructive, security-sensitive, privacy-sensitive, or materially changes cost.
- Do not claim a validation command works unless it was run or found in repo docs.
