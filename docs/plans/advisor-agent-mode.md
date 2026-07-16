# ExecPlan: Advisor Agent Mode

This plan follows `AGENTS.md` and `docs/PLAYBOOK.md`.

## Purpose / Big Picture

Make repo-aware ChatGPT agent mode the preferred default advisor workflow without removing the current prompt-only critique path. The end state is a safe default where Codex uses a DevSpace-style MCP bridge for advisor inspection, planning, and review when a narrow allowed root is configured, while prompt-only advisor mode remains available when agent-mode is unavailable, unsafe, or explicitly requested.

## Progress

- [x] Confirm current repo state and relevant files before editing.
- [x] Inspect current DevSpace CLI/docs or equivalent MCP bridge behavior before implementation.
- [x] Preserve prompt-only advisor mode and existing tests.
- [x] Design the review-only v1 agent-mode surface and safety policy.
- [x] Add default routing rules that prefer safe configured agent-mode.
- [x] Implement the first safe vertical slice.
- [x] Add root-validation and dry-run tests.
- [x] Add user-level setup config and secret preflight refusal for repo-aware handoff.
- [x] Add automatic sanitized review workspace generation for projects with blocked local files.
- [x] Update README, `external-advisor` skill docs, and setup guidance.
- [x] Run validation and review the final diff.

## Surprises & Discoveries

- DevSpace's documented tool surfaces can expose edit and shell capabilities. V1 therefore keeps this repo's automation review-first by generating handoff text and route selection only; it does not claim to mechanically disable every remote DevSpace tool.
- The local machine had Node/npm/npx available, but no trusted `devspace` executable on PATH during implementation. Tests use a fake bridge executable outside the project to prove deterministic route behavior without installing DevSpace.
- DevSpace-style bridges do not provide a per-file denylist for `.env`, HAR, key, wallet, browser-profile, or advisor transcript files inside an approved root, so this repo now runs its own local secret preflight and refuses agent-mode when those files are present.
- Safe refusal alone made agent-mode unusable in normal projects that contain `.env` or `.codex-advisor` state. The router now creates a sanitized review copy under `~/.codex/advisor-agent/workspaces/` when needed, then points ChatGPT at that copy.

## Decision Log

- Decision: Make agent-mode the preferred default advisor path when safe MCP setup is configured.
  Rationale: Repo-aware ChatGPT can inspect local facts directly and should give better critique for architecture, planning, and broad repo-analysis questions than prompt-only context.
  Date/Author: 2026-07-09 / Codex

- Decision: Keep prompt-only critique mode as a fallback and explicit mode, not the primary path.
  Rationale: Prompt-only mode is still needed when DevSpace/MCP is unavailable, unsafe, too broad, or unnecessary.
  Date/Author: 2026-07-09 / Codex

- Decision: Default agent mode to review-first and worktree-first.
  Rationale: ChatGPT can add value by inspecting, planning, and reviewing while Codex remains the primary local implementer unless the user explicitly grants edit authority.
  Date/Author: 2026-07-09 / Codex

- Decision: Make v1 inspect/plan/review only and do not enable ChatGPT edits or shell execution by default.
  Rationale: Local edit and shell authority materially changes the security posture and needs a separate explicit decision after the safe handoff/doctor flow exists.
  Date/Author: 2026-07-09 / Codex

- Decision: Do not store v1 agent-mode state under `.codex-advisor/`.
  Rationale: `.codex-advisor/` can contain transcripts, latest responses, project bindings, and local state, so it remains on the sensitive-path denylist.
  Date/Author: 2026-07-09 / Codex

- Decision: Require configured allowed roots and a trusted bridge executable before defaulting to agent-mode.
  Rationale: "Default agent-mode" should mean preferred when safely configured, not silent filesystem exposure.
  Date/Author: 2026-07-09 / Codex

- Decision: Keep setup passive for agent-mode.
  Rationale: Setup must not install DevSpace, launch MCP, open tunnels, write roots, or change local exposure without explicit user action.
  Date/Author: 2026-07-09 / Codex

- Decision: Store durable agent-mode allowed roots in a user-level config, not inside the repo.
  Rationale: A repository must not be able to self-authorize its own root for ChatGPT/DevSpace access.
  Date/Author: 2026-07-09 / Codex

- Decision: Run a fresh secret preflight before agent-mode is available.
  Rationale: A project can become unsafe after setup if someone adds `.env`, HAR/cookie/auth, key, wallet, browser-profile, symlink escape, or advisor transcript files under the exposed root.
  Date/Author: 2026-07-09 / Codex

- Decision: Use sanitized copies, not sanitized git worktrees, for blocked projects.
  Rationale: Worktrees can still expose tracked secrets, while a managed copy can omit sensitive paths, secret-looking content, symlinks, archives, databases, and generated dependency/build directories.
  Date/Author: 2026-07-09 / Codex

## Context and Orientation

Relevant files:
- `README.md`
- `AGENTS.md`
- `BUNDLED_SKILLS.md`
- `setup.sh`
- `setup.ps1`
- `codex-skill/external-advisor/SKILL.md`
- `codex-skill/external-advisor/scripts/advisor.py`
- `codex-skill/external-advisor/scripts/router.py`
- `codex-skill/external-advisor/scripts/context_pack.py`
- `codex-skill/external-advisor/scripts/agent_mode.py`
- `codex-skill/external-advisor/scripts/advisor_agent_setup.py`
- `codex-skill/external-advisor/scripts/advisor_safety.py`
- `tests/test-advisor-transport-recovery.sh`
- `tests/test-router.sh`
- `tests/test-security-regressions.sh`
- `tests/test-agent-mode.sh`

Current friction:
- The advisor has no implicit repository access and can only reason from context Codex sends.
- The desired default for serious critique is now repo-aware inspection when a safe root is configured, because prompt-only advice can miss local facts.
- Broad repo-aware access changes the threat model, especially around file reads, shell commands, logs, tunnels, and secrets.
- ChatGPT web MCP behavior and DevSpace CLI behavior are external dependencies and must be verified during implementation.

## Plan of Work

1. Inspect the current `external-advisor` skill docs and safety helpers to locate the best extension point.
2. Inspect current DevSpace installation/docs/CLI behavior if available locally; otherwise document assumptions and keep the integration adapter-based.
3. Define the agent-mode surface:
   - likely command shape: `scripts/agent_mode.py --doctor`, `--print-handoff`, and optional `--project-dir`
   - provider name: `devspace` or a compatible bridge adapter
   - default advisor routing prefers agent-mode when the current project has a validated allowed root and bridge configuration
   - prompt-only advisor mode remains available through an explicit flag/env var or automatic safe fallback
   - v1 review-only: no default ChatGPT edit or shell execution authority
4. Implement root and secret-boundary validation:
   - normalize and resolve symlinks before containment checks
   - reject or warn on `~`, `/`, drive roots, `.ssh`, browser profiles, HAR/cookie/auth dirs, `.codex-advisor`, `.env`, `.env.local`, OpenaiChat auth files, and likely key/wallet paths
   - verify project is under an allowed root
   - cover parent/child confusion, paths with spaces, and case-insensitive comparisons where relevant
   - do not print token/password/HAR contents
5. Implement a dry-run/doctor flow:
   - check Node/npm availability
   - check whether a local `devspace` executable is available
   - check whether `npx` exists without invoking remote packages
   - check Git/worktree availability
   - print setup guidance without opening a tunnel, launching DevSpace, contacting ChatGPT, or writing credentials
6. Implement a handoff generator:
   - produces a ChatGPT prompt for review-first workflow
   - states that ChatGPT must open one workspace, inspect first, avoid secrets, prefer worktree, and ask before edits/shell commands
   - includes current repo path only when user requested it and it is under an allowed root
7. Implement default selection logic:
   - prefer agent-mode for non-trivial advisor critique when configuration is valid
   - fall back to prompt-only when no allowed root exists, DevSpace/MCP is unavailable, safety validation fails, the request is trivial, or the caller explicitly requests prompt-only
   - report the selected route clearly without printing secrets or tunnel credentials
8. Update docs:
   - README: add default agent-mode vs prompt-only fallback explanation
   - `external-advisor/SKILL.md`: add concise routing guidance for default agent-mode and fallback prompt-only critique
   - setup notes for DevSpace-compatible bridges without silently exposing a root
9. Add tests and run validation.

## Milestones

### Milestone 1: Characterize Existing Contracts

Work:
- Read the current advisor CLI, router, context pack, safety helpers, setup scripts, and tests.
- Confirm that prompt-only fallback mode stays dependency-free and does not depend on DevSpace.
- Record any existing sensitive-path helpers that should be reused.

Validation:
- `git status --short`
- `python3 -m py_compile codex-skill/external-advisor/scripts/advisor.py codex-skill/external-advisor/scripts/router.py codex-skill/external-advisor/scripts/context_pack.py codex-skill/external-advisor/scripts/advisor_safety.py`

### Milestone 2: Design And Add Agent-Mode Safety Helpers

Work:
- Add a small module or script for agent-mode safety validation.
- Reuse existing advisor redaction/sensitive-path logic when practical.
- Add tests for allowed-root validation, secret-path denial, symlink escape, parent/child confusion, path names containing spaces, OpenaiChat auth files, HAR filenames, browser profile dirs, wallet/private-key patterns, and case-insensitive comparisons where relevant.

Validation:
- `python3 -m py_compile codex-skill/external-advisor/scripts/agent_mode.py codex-skill/external-advisor/scripts/advisor_safety.py`
- `./tests/test-agent-mode.sh`
- `./tests/test-security-regressions.sh`

### Milestone 3: Add Dry-Run Doctor And Handoff Flow

Work:
- Add a dry-run/doctor command that validates local prerequisites without opening a public tunnel.
- Add a handoff generator for ChatGPT/DevSpace review-first workflow.
- Ensure the command does not print secrets, does not start remote exposure, does not run `npx @waishnav/devspace`, and does not launch DevSpace by default.
- Add a deterministic handoff golden-output test.

Validation:
- `python3 codex-skill/external-advisor/scripts/agent_mode.py --doctor --project-dir . --allowed-root .`
- `python3 codex-skill/external-advisor/scripts/agent_mode.py --print-handoff --project-dir . --allowed-root .`
- `./tests/test-agent-mode.sh`

### Milestone 4: Add Default Route Selection

Work:
- Add route-selection logic so suitable non-trivial advisor use prefers agent-mode when a validated allowed root and bridge configuration exist.
- Keep prompt-only critique as an explicit mode and automatic fallback.
- Ensure normal prompt-only advisor execution remains dependency-free and does not import, require, launch, or validate DevSpace, Node, MCP, or tunnels unless the selected route is agent-mode.
- Add tests for configured agent-mode selection, unsafe fallback, unavailable bridge fallback, explicit prompt-only override, and route reporting.

Validation:
- `./tests/test-agent-mode.sh`
- `./tests/test-router.sh`
- `./tests/test-security-regressions.sh`

### Milestone 5: Update Skill And Setup Documentation

Work:
- Update `README.md` with a "Default Agent Mode" section and prompt-only fallback notes.
- Update `codex-skill/external-advisor/SKILL.md` with concise usage guidance.
- Mention that setup must not start DevSpace, open a tunnel, or expose a root without explicit user action.
- Include `setup.ps1` in scope if setup behavior changes so Windows guidance stays consistent.

Validation:
- Inspect rendered Markdown structure by reading changed sections.
- `git diff --check`

### Milestone 6: Final Regression Pass

Work:
- Run existing advisor/router/security checks.
- Review the final diff for accidental secrets, generated files, broad setup exposure, or broken prompt-only fallback behavior.
- Update this plan's progress and outcomes.

Validation:
- `./tests/test-advisor-transport-recovery.sh`
- `./tests/test-router.sh`
- `./tests/test-security-regressions.sh`
- `./tests/test-agent-mode.sh`
- `git status --short`
- `git diff --check`

## Risks and Rollback

- Risk: Agent mode blurs the existing safe advisor boundary.
  Mitigation: Require configured narrow allowed roots, keep prompt-only fallback, keep v1 review-only, and document the different threat model.

- Risk: Users expose too much of the filesystem through MCP.
  Mitigation: Reject broad roots in tooling where possible and document narrow allowlists prominently.

- Risk: ChatGPT shell access can run commands outside workspace file containment.
  Mitigation: V1 review-only default, explicit shell warning, no shell logging with secrets, and worktree-first guidance.

- Risk: DevSpace CLI or ChatGPT Developer Mode changes.
  Mitigation: Treat DevSpace as an external dependency, validate at runtime, keep implementation adapter-based, and fall back to prompt-only critique.

- Risk: New setup changes break the existing advisor flow.
  Mitigation: Do not modify normal setup/start scripts unless necessary; run existing advisor regression tests.

Rollback:
- Remove or disable the new agent-mode script/docs while leaving `advisor.py`, `conclave.py`, router, verifier, setup, and transcript sync untouched.

## Outcomes & Retrospective

- Added `agent_mode.py` with allowed-root validation, bridge executable checks, dry-run doctor output, worktree detection, and deterministic review-first handoff generation.
- Added `advisor_agent_setup.py` with user-level allowed-root config writing, exact project-root defaults, config-path safety checks, and no DevSpace/tunnel/ChatGPT side effects.
- Added agent-mode secret preflight scanning for `.env*`, HAR/cookie/auth files, key material, wallets/seeds, browser profiles, symlink escapes, secret-looking config contents, and unsafe `.codex-advisor` state.
- Added automatic sanitized review workspace generation under `~/.codex/advisor-agent/workspaces/`, including deterministic rebuilds, conservative skip rules, a Markdown marker, and `SANITIZED_WORKSPACE_MANIFEST.json`.
- Added `advisor_agent_connect.py` so an explicit user/Codex command can validate the local project, start a trusted DevSpace bridge, print the exact ChatGPT `/mcp` connector URL, and emit the review-first handoff. Setup, doctor, and router dry runs remain passive.
- Updated `router.py` so suitable non-trivial advisor routes prefer `agent-mode` only when safe allowed-root and bridge configuration validate; otherwise existing prompt-only advisor/conclave/verifier behavior remains the fallback.
- Added `--prompt-only`, agent-mode route flags, and route metadata for transparent fallback/selection.
- Updated README, skill instructions, setup chmods, and tests.
- Refreshed the installed `~/.codex/skills/external-advisor` copy while preserving `advisor-config.json`, so new Codex sessions can discover the new agent-mode instructions/scripts.
- Validation passed for compile, agent-mode, router, security regressions, and advisor transport recovery.
