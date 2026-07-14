# PRD: Advisor Agent Mode

## Problem

The current `external-advisor` workflow is intentionally prompt-only. That keeps it safe and predictable, but it means ChatGPT only sees the snippets, context packs, diffs, logs, or summaries Codex sends. For architecture reviews and broad repo analysis, this can make the advisor miss local facts or suggest unverified file paths, commands, and root causes.

A separate public pattern now exists: self-hosted MCP bridges such as DevSpace can let ChatGPT open approved local project folders, read/search files, inspect diffs, and run commands after explicit approval. This can turn ChatGPT from a passive second-pass advisor into the preferred repo-aware advisor path for critique and architecture questions.

## Goal

Make agent-mode the default advisor workflow when a safe DevSpace-style MCP bridge and narrow allowed project root are configured, while preserving the existing prompt-only advisor as an explicit critique-only fallback.

When complete, Codex should have durable scripts/docs/instructions for:

- keeping current prompt-only advisor mode available and safe
- preparing a DevSpace-style local MCP connection for selected project roots
- routing normal critique/architecture advisor use toward repo-aware inspect, plan, and review when safe
- preferring Git worktrees before direct edits to an active checkout
- enforcing or documenting safety rails around roots, secrets, shell access, and logging
- keeping v1 review-only by default: ChatGPT may inspect and review through the documented workflow, but this repo must not enable direct ChatGPT edit or shell authority by default

## Non-Goals

- Do not replace `advisor.py`, `conclave.py`, router, verifier, or transcript sync with agent mode.
- Do not expose any repo path to ChatGPT unless an allowed root has already been configured and validated.
- Do not vendor or fork DevSpace unless a later plan explicitly chooses that.
- Do not expose broad roots such as `~`, `/`, home-directory document stores, browser profiles, `.ssh`, wallet folders, HAR/cookie directories, or secret stores.
- Do not send `.env`, HAR, cookies, tokens, private keys, wallet keys, customer data, or unrelated private files to ChatGPT.
- Do not make remote tunnel setup automatic in a way that exposes a machine without explicit user action.
- Do not allow ChatGPT shell/edit access by default in active checkouts.
- Do not write v1 agent-mode state under `.codex-advisor/`; that directory remains denied because it can contain advisor transcripts, latest responses, project bindings, and local state.
- Do not let a repo-local config self-authorize agent-mode. Durable allowed roots belong in a user-level config outside the project.
- Do not run `npx`, install packages, launch DevSpace, open tunnels, or contact ChatGPT from a dry-run/doctor command.

## Users / Stakeholders

- Primary user: the local developer running Codex and ChatGPT on the same machine.
- Secondary users: future Codex sessions maintaining this advisor repo.
- Systems affected: `external-advisor` skill, setup docs, local project `.codex-advisor` state, default advisor routing, DevSpace/MCP configuration, and Git worktree workflows.

## Requirements

- Agent mode must become the preferred default advisor path for non-trivial critique, planning, architecture, and broad repo-analysis requests when a safe allowed root and MCP bridge are configured.
- Current prompt-only advisor mode must remain available and continue to work without DevSpace, MCP, tunnels, Node, or ChatGPT Developer Mode.
- Agent mode must not silently grant new filesystem access; setup must require explicit allowed roots and a successful safety validation.
- The first implementation should integrate with DevSpace as an external dependency or provide a clear adapter boundary for a DevSpace-compatible MCP bridge.
- Agent mode must support a review-first flow where ChatGPT can inspect/plan/review while Codex remains the default implementer.
- Agent mode v1 must be inspect/plan/review only by default. Any future direct edit or shell authority must require a separate explicit product decision and tests.
- Agent mode must document and prefer worktree mode for edits, with active-checkout edit mode requiring an explicit user decision.
- Agent mode must refuse or warn on broad allowed roots such as `~`, `/`, drive roots, and known sensitive directories.
- Agent mode must include a denylist or safety checklist for `.env`, HAR/cookie/auth files, `.codex-advisor`, private keys, wallet files, browser profiles, and customer data.
- Agent mode must refuse by default when the selected project contains obvious sensitive files or symlink escapes inside the exposed root, including `.env*`, HAR/cookie/auth files, key material, wallet/seed files, browser profiles, and advisor transcript/conversation state.
- Agent mode may recover from those findings by generating a sanitized review workspace outside the repo, then opening that copy instead of the original checkout.
- Agent mode root validation must normalize and resolve symlinks, enforce ancestor/descendant containment, handle project paths containing spaces, and account for case-insensitive path comparisons where relevant.
- Agent mode must warn that shell commands run with the local user account and are not contained by workspace file-tool path checks.
- Agent mode must keep shell command logging off when commands may contain secrets.
- Agent mode docs must explain the public tunnel requirement, owner password approval, and why the tunnel URL is not a secret.
- Agent mode must include a dry-run/doctor path that can validate local prerequisites without opening a tunnel, launching DevSpace, running remote packages through `npx`, contacting ChatGPT, or starting network exposure.
- Setup and docs must make clear that this is a repo-aware local MCP advisor workflow, not a quota bypass, not a replacement for Codex verification, and not a reason to trust advisor claims without local evidence.

## Acceptance Criteria

- Existing advisor tests still pass, including:
  - `./test-advisor-transport-recovery.sh`
  - `./test-router.sh`
  - `./test-security-regressions.sh`
- A new or updated test covers agent-mode root validation and denies at least `~`, `/`, `.ssh`, `.env`, HAR/cookie paths, and `.codex-advisor`.
- New tests cover symlink escape, allowed-root parent/child confusion, `.env.local`, OpenaiChat auth filenames, HAR filenames, browser profile directories, wallet/private-key patterns, path names containing spaces, and case-insensitive comparisons where the platform requires them.
- New or updated tests cover user-level setup config, config path outside the project, secret preflight fallback, and prompt-only fallback when agent-mode is unsafe.
- New or updated tests cover automatic sanitized workspace generation, omission of `.env*`, manifest creation, handoff mode `sanitized_copy`, and prompt-only fallback when sanitization is explicitly disabled.
- A new dry-run/doctor command or documented script can report:
  - whether Node/npm are available
  - whether a `devspace` executable is available locally
  - whether `npx` is available without invoking remote packages
  - the configured allowed roots
  - whether the current project is safely under an allowed root
  - whether worktree mode is available
- The dry-run/doctor command does not launch DevSpace, open tunnels, run `npx @waishnav/devspace`, contact ChatGPT, create remote exposure, or write credentials.
- A deterministic handoff test proves the generated ChatGPT prompt says inspect/review first, avoid secrets, ask before edits/shell, prefer worktrees, and keep Codex as default implementer.
- Documentation explains the safe default workflow:
  1. Configure a narrow allowed project root.
  2. Prefer agent-mode for non-trivial advisor critique when the MCP bridge is available.
  3. Fall back to prompt-only critique when agent-mode is unavailable, unsafe, or explicitly disabled.
  4. Prefer worktree/review-first.
  5. Let ChatGPT inspect/plan/review.
  6. Let Codex implement unless user explicitly grants ChatGPT edit/run authority.
- The `external-advisor` skill instructions make agent-mode the preferred default for suitable non-trivial requests, while keeping prompt-only critique available.
- README documents the difference between default repo-aware agent mode and prompt-only fallback mode.
- Normal `advisor.py` calls do not import, require, launch, or validate DevSpace, Node, MCP, or tunnels.
- No HAR, cookie, token, `.env`, transcript state, tunnel token, owner password, public tunnel URL, or sensitive shell command output is committed, written to repo files, included in context packs, or printed in test output.

## Constraints

- Preserve existing environment variables and CLI behavior for prompt-only advisor calls, except where new route-selection flags are explicitly added.
- Preserve `gpt-5-6-thinking` plus `thinking_effort=max` as the normal advisor route and `ADVISOR_THINKING_EFFORT=pro-extended` as the hard-question route.
- Preserve `.codex-advisor/` state layout for normal advisor, conclave, router, verifier, and project binding.
- Preserve setup scripts for g4f/HAR advisor use unless a change is explicitly needed for default agent-mode setup or detection.
- Use project-local docs and tests; do not rely on this chat history.
- Treat DevSpace as an external, fast-moving dependency; inspect its current CLI/docs before implementation and avoid hard-coding assumptions that can be validated at runtime.
- Keep the implementation adapter-based and avoid hard-coding the `@waishnav/devspace` package name in code paths that should work with any compatible bridge, except in optional documentation examples after current docs are verified.

## Resolved Decisions

- The first implementation routes through `scripts/agent_mode.py` plus `scripts/router.py`; `advisor.py` remains prompt-only and dependency-free.
- General setup remains passive for agent mode. It does not install DevSpace, write allowed roots, launch DevSpace, open tunnels, or contact ChatGPT.
- The explicit `advisor_agent_setup.py` helper may write the current validated root to user-level config only, never repo-local config, and never launches DevSpace, opens tunnels, contacts ChatGPT, or writes credentials.
- V1 writes no repo-local agent-mode state. `.codex-advisor/` transcript/conversation state remains denied for agent-mode exposure; only route-log bookkeeping files are allowed.
- If blocked files are present, V1 creates a managed sanitized review copy under `~/.codex/advisor-agent/workspaces/`, skips sensitive/generated/bulk files, writes a manifest, and labels the handoff as `sanitized_copy`.
- Tunnel guidance is provider-neutral; users can choose Cloudflare Tunnel, Tailscale Funnel, ngrok, Pinggy, or another HTTPS reverse proxy.
- Review-only v1 is enforced by this repo's generated handoff and router behavior, not by modifying DevSpace itself. DevSpace may still expose edit/shell tools, so connected ChatGPT must be treated as a trusted local coding partner.
