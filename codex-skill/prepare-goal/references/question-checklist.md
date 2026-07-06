# Question Checklist

Ask only the questions needed to make the PRD, execution plan, and final `/goal` actionable. If the answer can be safely inferred from repo conventions, infer it and record the assumption.

## Always Consider

- Objective: What should be true when the work is done?
- Scope: Which feature, module, route, package, or workflow is in scope?
- Non-goals: What must not change?
- Acceptance: How will the user know the work is complete?
- Constraints: Which public contracts, schemas, env vars, route URLs, response shapes, data formats, or permissions must be preserved?
- Validation: Which commands, tests, manual checks, or deployment gates prove success?

## Ask When Missing

Feature work:
- Who is the user or stakeholder?
- What user-visible behavior should exist?
- Which current behavior must remain unchanged?
- Are there localization, accessibility, analytics, or admin requirements?

Refactor or migration:
- Is the goal behavior-preserving or behavior-changing?
- Is a staged rollout, rollback path, or compatibility layer required?
- Are database migrations, data backfills, or external API contracts involved?

Debugging:
- What is the failing command, error text, route, input, or reproduction path?
- What is the expected behavior?
- Is there recent context such as a branch, deploy, dependency update, or migration?

Security, privacy, payments, and auth:
- What data, permission, payment, auth, or secret boundary is involved?
- Is user approval required before modifying the flow?
- What audit, logging, rate limiting, encryption, or access-control acceptance criteria are required?

Release or deployment:
- What branch, environment, and deployment target are involved?
- What preflight checks, rollback steps, and post-release smoke checks are required?
- Should Codex commit, push, or only prepare local files?

## Good Question Style

- Ask at most 7 questions before file creation.
- Use direct questions, not broad architecture surveys.
- Group questions by decision impact.
- If continuing with assumptions is safe, state the assumption and proceed.
- If a question blocks safe progress, do not create misleading PRD or plan content; mark the plan blocked until answered.
