# PLAYBOOK.md

## How We Refactor

1. Preserve existing public behavior first.
2. Find a vertical slice that can be tested independently.
3. Add characterization tests when behavior is unclear.
4. Move logic behind focused modules or stable contracts.
5. Keep CLIs, setup scripts, and skill instructions explicit.
6. Run targeted tests after each milestone.
7. Record decisions and surprises in the active plan.

## How We Debug

1. Reproduce the failure or inspect the exact command path.
2. Find the smallest failing command or script.
3. Explain the suspected cause before patching.
4. Patch narrowly.
5. Prove the fix with targeted validation.
6. Add regression coverage for the bug class.

## How We Choose Architecture

Prefer boring, inspectable workflows over hidden automation. Preserve current advisor behavior unless a PRD explicitly changes it. Repo-aware advisor agent mode may become the default advisor path only after an explicit safe setup exists; it must stay constrained by explicit filesystem roots and safe to disable without breaking prompt-only critique mode.

## Advisor And Agent Boundaries

1. Repo-aware advisor agent mode is the preferred default when a safe allowed-root MCP setup is configured.
2. Prompt-only advisor mode remains available as an explicit critique-only fallback.
3. The advisor has no repo access unless agent mode grants it through a documented local tool bridge and a narrow allowed root.
4. Treat ChatGPT with local tools as a trusted coding partner with local-machine access, not as a passive advisor.
5. Prefer worktrees and review-first flows before enabling direct edits in an active checkout.
