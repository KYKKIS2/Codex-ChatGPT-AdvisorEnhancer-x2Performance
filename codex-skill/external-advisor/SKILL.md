---
name: external-advisor
description: "Use automatically when Codex is asked for broader judgment rather than direct coding execution: architecture decisions, what to do next, strategy, planning, tool or model choice, tradeoff analysis, design direction, high-impact recommendations, or important guidance where a second-pass critique from an external reasoning model would materially improve quality. Do not use for routine implementation/debugging that Codex can handle directly. Also use when the user asks to consult ChatGPT Pro, an external advisor, OpenAI API, a local OpenAI-compatible endpoint, or gpt4free/g4f before answering."
---

# External Advisor

Use this skill as a second-pass critique and verification layer. It does not replace Codex's judgment and it must not be used to bypass access controls or expose private credentials.

For difficult judgment tasks, use the advisor as a bounded conclave rather than a single generic second opinion. Codex remains the orchestrator and final decision maker.

Prompt-only advisor calls do not have implicit access to the local repository, filesystem, terminal, git state, logs, tests, screenshots, or Codex's observations. They only see the prompt, explicit context files, context packs, and synced advisor-chat transcript content that Codex sends. Do not imply that a prompt-only advisor inspected repo files or runtime state unless those exact artifacts were included in the advisor call.

Repo-aware advisor agent-mode is the preferred default for non-trivial critique, planning, architecture, and broad repo-analysis requests when a safe allowed root and a registered, live DevSpace-compatible MCP bridge are configured. `advisor_agent.py` runs the review in an isolated ChatGPT conversation, requires bounded read-only repository inspection, and verifies that exact conversation's MCP calls before accepting the answer. It does not replace Codex verification, and Codex remains the default implementer.

If local `.codex-advisor` state has been deliberately synced to a ChatGPT web conversation that already has an enabled DevSpace/MCP connector for the project, continuing that exact conversation through the local g4f/OpenAI-compatible adapter can be repo-aware, like typing into the same ChatGPT web chat. Codex may then ask the advisor to use the connected DevSpace repo tools for targeted inspection. This only applies to the same saved conversation id with the connector still enabled, the tunnel/DevSpace bridge still running, and connector auth still valid. Do not assume repo access for a new chat, a different conversation key, a temporary advisor call, a disabled connector, or a dead tunnel.

## Decision

Use the advisor when the answer is about judgment, direction, architecture, strategy, or tradeoffs rather than direct code execution, or when the user explicitly asks for ChatGPT/OpenAI/g4f guidance.

Default to using it for broad direction and judgment questions, such as:

- "What should I do next?"
- "Which direction should I take?"
- "Can you advise me on this?"
- "What is the better architecture or strategy?"
- "What should I use for this?"
- "What are the risks, tradeoffs, and best next steps?"
- "I am unsure whether this approach is good."

Skip the advisor for routine code edits, implementation work, direct debugging, simple terminal answers, fast status updates, or when it would require sending secrets, private keys, proprietary data, or unrelated user files to an external model.

For repo-aware critique, prefer `scripts/router.py --execute`. The router selects `agent-mode` for a single repo-aware reviewer and upgrades multi-role conclave decisions to `agent-conclave` only after the current connector has completed one successful fail-closed ChatGPT MCP turn. Local tunnel readiness alone does not prove that the URL was added to ChatGPT. Fresh or rotated connectors therefore stay on prompt-only lanes until a direct `advisor_agent.py` call verifies the attachment; `--force-route agent-mode` can bootstrap that diagnostic. Agent routes also require a narrow allowed root, a trusted bridge, a connector-ready runtime, and either a clean secret preflight or a generated sanitized review workspace. If a safety or readiness check fails, or if `--prompt-only`/`ADVISOR_AGENT_MODE=off` is set, the router falls back to the existing prompt-only advisor, conclave, or verifier path.

The four primary lanes are:

- `scripts/advisor.py`: one prompt-only reasoning pass; it sees only supplied context.
- `scripts/conclave.py`: multiple prompt-only specialist passes plus synthesis.
- `scripts/advisor_agent.py`: one repo-aware read-only specialist using the registered MCP connector.
- `scripts/agent_conclave.py`: multiple isolated repo-aware specialists, followed by prompt-only synthesis. Role subprocesses may launch concurrently, while the machine-wide remote FIFO admits two ChatGPT turns by default; each admitted role receives one disposable g4f worker.

Use `scripts/conclave.py` instead of `scripts/advisor.py` when the task would benefit from multiple specialist viewpoints, such as:

- architecture or strategy decisions with meaningful tradeoffs
- plans that should be attacked before implementation
- security/privacy-sensitive design
- model, framework, or tool choices with multiple plausible options
- high-impact recommendations where Codex should compare alternatives before answering
- evidence checks where a verifier should identify commands, tests, inspections, and expected signals

Use `scripts/agent_conclave.py` for hard architecture, security, broad code-review, or high-impact strategy tasks where independent specialists should inspect the repository. Each role must prove its own `open_workspace` plus read/search activity from its exact ChatGPT conversation. When ChatGPT retains a read/search result but omits its request node, the wrapper additionally requires a matching private DevSpace record under the unique workspace id returned by that conversation's open call. An unmatched failed graph-only open request is tolerated only when that private workspace-attributed log proves exactly one real successful open and every normal safety check passes. An uncorrelated shared log window is observability only and is never accepted as role evidence.

Use `scripts/router.py` when Codex needs to decide the path from the task shape. It chooses among no-advisor, agent-mode, agent-conclave, single-advisor, conclave, verifier loop, and machine-json verifier loop.

Router prompt classification never treats the mere presence of authentication, token, privacy, or security topic words as sensitive data, and those words do not select a special lane. Normal task-shape signals choose the route; force a security mode explicitly when a security-specialist conclave is actually needed. Boundary-aware term matching prevents fragments such as `auth` in `authoritative` or `rl` in `world` from changing the route. Prompt-only calls transmit the prompt, generated context-pack data, and explicitly selected context verbatim, including selected paths outside the project, because Codex controls that bounded payload. `ADVISOR_PROMPT_PROTECTION=true` restores legacy prompt redaction and protected-context filtering as an opt-in diagnostic. Repo-aware calls always retain sanitized-workspace, secret-scan, and denied-path controls because the remote agent can discover files beyond the explicit prompt.

Use `scripts/advisor_agent_connect.py serve --project-dir .` when the user wants Codex to automate the local side of repo-aware ChatGPT agent-mode. The connect helper validates/writes the generated sanitized workspace root in the user-level config, verifies the pinned DevSpace read-only patch, starts DevSpace plus an optional managed Cloudflare quick tunnel, checks both local and public MCP readiness, prints the exact `https://.../mcp` ChatGPT connector URL, and prints the review handoff. It records managed child processes before readiness, so an interrupted startup can be recovered with the normal lifecycle commands. It never edits ChatGPT account settings; the user still pastes the printed URL into ChatGPT Settings -> Apps & Connectors -> Developer Mode -> Create app/connector. Use `scripts/advisor_agent_connect.py status --project-dir .` to verify readiness and `scripts/advisor_agent_connect.py stop --project-dir .` to stop both managed processes.

Use `scripts/advisor_agent_setup.py --auto --project-dir .` only when you want to write the allowed-root config without starting DevSpace. If the project contains blocked local files, setup can generate a sanitized workspace under `~/.codex/advisor-agent/workspaces/` and record that generated root too. Use `scripts/agent_mode.py --doctor` to validate local agent-mode readiness without launching DevSpace, opening a tunnel, invoking `npx`, contacting ChatGPT, or writing credentials. Use `scripts/agent_mode.py --print-handoff` to generate only the review-first ChatGPT handoff. The handoff tells ChatGPT to open the pinned path in checkout mode, inspect and review only, avoid secrets, and stay within the sanitized snapshot; worktree and base-ref opens are unavailable.

Use `scripts/context_pack.py` before advisor calls that need repository context. It builds a compact task bundle with the task, draft/plan, selected files, git status/diff, failures, constraints, and existing advisor memory summaries.

Use context packs and `--context-file` deliberately. If Codex asks the advisor a high-level question without attaching files, the advisor can still provide useful strategy, but any file names, modules, commands, metrics, or root causes it mentions are hypotheses until Codex verifies them locally.

Use `scripts/critique_final.py` before sending important user-facing advice. Codex should draft, ask the critic advisor to attack the draft, then revise the final answer itself.

Use `scripts/verifier_loop.py` when the work needs evidence, especially after failed tests or a risky patch. It asks the verifier for a checklist, runs explicit `--command` checks by default, then asks the verifier to interpret the real command output. Use `--run-suggested` only after Codex reviews and accepts running safe advisor-suggested commands.

Use `scripts/memory_manager.py` to initialize searchable advisor memory, record accepted/rejected advice and outcomes, and summarize stale or low-confidence decisions.

Use `scripts/eval_harness.py` to compare Codex-only, single-advisor, conclave, and critic/verifier lanes across architecture, code-review, debugging, and model-choice tasks. Use `--dry-run` for structure checks; live advisor lanes require the local API.

## Workflow

1. For non-trivial repo-analysis or architecture critique, first prefer `scripts/router.py --execute` so configured agent-mode can be selected. If agent-mode is unavailable or unsafe, fall back to prompt-only advisor context.
2. Gather the smallest useful context: the user's request, draft answer or plan, and only the files or snippets needed for critique. State clearly in the prompt when the advisor has not been given repo files and should reason only from the supplied summary.
3. Do not send secrets, credentials, private keys, wallet keys, tokens, `.env` values, HAR contents, cookies, customer data, or unrelated private files. Context packs filter common sensitive paths and redact obvious secret-looking values, but Codex is still responsible for choosing safe context.
4. Repo-aware agent-mode has a stronger local safety gate because the bridge can inspect files directly. The default is always a generated content-hashed review snapshot under `~/.codex/advisor-agent/workspaces/`; the original checkout is not exposed by the normal route. All `.codex-advisor` state is excluded because it can contain prompts, transcripts, conversation ids, response fragments, or route logs. The exposed manifest contains only counts, hashes, and provenance; detailed omitted/redacted path lists stay in a mode-`0600` private manifest outside the exact MCP root. Before every turn, the wrapper atomically pins DevSpace to the exact current snapshot; historical generations and staging directories remain outside the MCP-readable boundary. If snapshot creation, pinning, or validation fails, fall back to prompt-only.
5. Treat sanitized workspaces as incomplete review copies. They omit `.git`, dependency/cache/build directories, `.env*`, HAR/cookie/auth files, key material, wallet/seed files, browser profiles, advisor state, symlinks, FIFOs/sockets/devices and other non-regular entries, binary files, archives, databases, oversized files, and text that cannot be safely redacted. Secret-looking text is redacted when the post-redaction scan succeeds. This is not a general PII or customer-data classifier; obtain data-owner approval for repositories with customer records or unusual confidential data. Each published generation is planned under its generation lock, checked with repeated full source-tree and Git-provenance scans, hash-verified, recorded in a complete omission/redaction manifest, and made read-only. Codex must verify final facts and apply edits in the original checkout.
6. Repo-aware agent calls are mechanically review-only. The patched DevSpace server verifies that its runtime registration set is exactly workspace-open, read, grep, glob, and list; it advertises checkout mode only and rejects worktree/base-ref opens. Shell and mutation tools are unavailable; `--allow-shell` is rejected.
7. Repo-aware agent calls default to `--timeout 0 --queue-timeout 0`: after the current prompt or stream is observed within the bounded acceptance-discovery window, the wrapper stays open until ChatGPT produces a real final end-of-turn response and waits as long as necessary for same-conversation coordination. A turn that never becomes observable fails closed instead of retaining locks forever. Positive timeout values are explicit operator limits. Keep the command in the foreground and let the open request wait; do not repeatedly poll ChatGPT.
8. If using a local OpenAI-compatible server, set `ADVISOR_PROVIDER=openai-compatible`, `ADVISOR_BASE_URL`, and `ADVISOR_MODEL`. Set `ADVISOR_API_KEY` only when that compatible endpoint requires a token; `OPENAI_API_KEY` is not forwarded to arbitrary compatible endpoints by default.
9. Before calling the advisor, check whether `http://127.0.0.1:8080/v1/models` is reachable.
10. If it is not reachable, automatically start the local g4f API before continuing:
   - First read `advisor-config.json` from this skill folder and use its `start_g4f` path if present.
   - If `ADVISOR_SETUP_DIR` is set, check `$env:ADVISOR_SETUP_DIR\start-g4f.ps1`.
   - Otherwise prefer `.\start-g4f.ps1` in the current working directory, then parent directories.
   - The starter script should install `vendor/gpt4free` automatically by running setup if it is missing.
   - Start it in the background with `Start-Process` and `-WindowStyle Hidden`, then wait until `http://127.0.0.1:8080/v1/models` responds.
11. If using official OpenAI, set `ADVISOR_PROVIDER=openai`, `OPENAI_API_KEY`, and optionally `ADVISOR_MODEL` plus `ADVISOR_REASONING_EFFORT`.
12. Run `scripts/advisor.py` with `--prompt` or stdin for explicit prompt-only critique, or when router fallback selects that path.
13. Local advisor calls automatically use the machine-wide coordinator and g4f supervisor when its manifest is present. Keep `ADVISOR_BASE_URL` on the configured base URL; do not select worker ports manually. Repo-aware wrappers require the local OpenAI-compatible provider and reject non-loopback base URLs before sending repository-derived prompts; loopback requests bypass proxies, reject redirects, and all local g4f/DevSpace listeners bind to literal `127.0.0.1`. Prompt-only lanes may still use an explicitly configured official or remote provider. Calls targeting the same state file or conversation id are serialized, including separate state files that reference one conversation; a first-turn state lock upgrades to the returned conversation id before persistence. An unknown-conversation first turn temporarily owns every known remote slot until that binding is established, preventing a cross-state lock-upgrade deadlock; normal configured concurrency resumes afterward. A separate FIFO uses the live supervisor's authoritative remote capacity (two by default), keeps the oldest ticket through disposable-worker startup and request preflight, and applies start pacing immediately before the actual turn submission; waiting calls generate no remote traffic. Each admitted call gets a newly started isolated g4f process that is terminated when the call exits. HTTP 429 records a shared cooldown and temporarily serializes new turns. Idempotent remote GETs use `Retry-After`-aware jittered exponential backoff; each wrapper invocation attempts its non-idempotent turn-submission POST exactly once, and every POST-side error fails closed instead of risking duplicate ChatGPT branches. Do not call `/v1/chat/completions` directly because that bypasses remote admission, lifecycle cleanup, coordination, transcript recovery, and model-route checks.
14. For local OpenAI-compatible calls, the script persists the returned `conversation` object by default under `.codex-advisor` in the nearest Git repo root or current working directory. Later Codex sessions in the same repo continue the same ChatGPT advisor chat.
15. Before and after each persistent local advisor call, the script syncs the remote ChatGPT conversation when possible and writes `.codex-advisor/transcript.json` plus `.codex-advisor/transcript.md`. Codex may inspect those files when it needs the advisor chat history.
16. If `.codex-advisor/project.json` exists, advisor calls should pass its normalized `chatgpt_project_id`/`g-p-...` id to g4f so new ChatGPT chats are created inside that ChatGPT Project. Default local state moves under `.codex-advisor/projects/<g-p-id>/`.
17. Repo-aware agent runners disable automatic ChatGPT Project creation. They reuse an existing binding when present but will not create duplicate Projects merely to run an isolated review conversation.
18. If no project binding exists, persistent non-temporary prompt-only local advisor calls auto-create a private ChatGPT Project named from the repo/folder and write `.codex-advisor/project.json`. Set `ADVISOR_AUTO_CREATE_PROJECT=false` to disable this.
19. To create and bind manually, run `scripts/project_bind.py --create --name "my-project"`. To bind an existing Project, run `scripts/project_bind.py --url "https://chatgpt.com/g/g-p-.../project"`, or set `ADVISOR_CHATGPT_PROJECT_URL`/`ADVISOR_CHATGPT_PROJECT_ID`; the advisor persists the normalized Project id into `.codex-advisor/project.json` on use.
20. To migrate old `.codex-advisor/conversation.json` state after pulling updates, run `scripts/project_migrate.py --url "https://chatgpt.com/g/g-p-.../project" --archive-root`, or omit `--url` to infer a Project id from old remote conversation metadata when possible. Use `--create-missing --archive-root` to create a new private Project when nothing can be inferred.
21. Set `ADVISOR_CONVERSATION_KEY` only when multiple advisor chats are needed in the same folder; keyed state is project-scoped under `.codex-advisor/conversations/` or `.codex-advisor/projects/<g-p-id>/conversations/`. The key is sanitized into a path-safe slug.
22. Set `ADVISOR_STATE_PATH` when a caller needs an explicit project-local state file, such as `.codex-advisor\roles\critic\conversation.json`.
23. Do not set `ADVISOR_TEMPORARY=true`, `ADVISOR_PERSIST_CONVERSATION=false`, or `ADVISOR_SYNC_REMOTE=false` for normal advisor calls. Those flags disable transcript recovery and can make g4f tail-fragment transport bugs look like bad advisor answers.
24. Use `ADVISOR_TEMPORARY=true`, `ADVISOR_PERSIST_CONVERSATION=false`, or `ADVISOR_SYNC_REMOTE=false` only for deliberate throwaway diagnostics where losing transcript recovery is acceptable.
25. For failed tests or evidence-heavy work, prefer `scripts/verifier_loop.py` over plain `conclave.py --mode verification` because it connects verifier advice to actual command output. The verifier runs commands with `shell=False` and a constrained allowlist; use `--allow-unsafe-commands` only as an explicit escape hatch.
26. Treat the result as advisory. Verify facts, reject weak advice, and incorporate only the parts that improve the final answer. In user-facing answers, distinguish advisor-derived guidance from facts Codex confirmed by reading files, running commands, or inspecting artifacts.

## Auto-Start Command

Use this PowerShell pattern when the local endpoint is down and `start-g4f.ps1` is available:

```powershell
$configPath = Join-Path $HOME ".codex\skills\external-advisor\advisor-config.json"
$script = if (Test-Path $configPath) {
    (Get-Content -Raw $configPath | ConvertFrom-Json).start_g4f
} else {
    (Resolve-Path .\start-g4f.ps1).Path
}
$shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
Start-Process $shell -WindowStyle Hidden -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$script`"")
```

Then poll `http://127.0.0.1:8080/v1/models` for up to 60 seconds before running `scripts/advisor.py`.

## Commands

Local OpenAI-compatible endpoint:

```powershell
$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:8080/v1"
$env:ADVISOR_MODEL = "gpt-5-6-thinking"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\advisor.py --prompt "Review this draft answer: ..."
```

Repo-aware agent-mode doctor and handoff:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\advisor_agent_connect.py serve --project-dir . --public-base-url "https://your-tunnel.example.com"
python $HOME\.codex\skills\external-advisor\scripts\advisor_agent_connect.py stop --project-dir .
python $HOME\.codex\skills\external-advisor\scripts\advisor_agent_setup.py --auto --project-dir .
python $HOME\.codex\skills\external-advisor\scripts\agent_mode.py --doctor --project-dir .
python $HOME\.codex\skills\external-advisor\scripts\advisor_agent.py --project-dir . --prompt "Inspect the repo and review this architecture decision."
python $HOME\.codex\skills\external-advisor\scripts\agent_conclave.py --project-dir . --mode architecture --prompt "Run independent repo-aware architecture and risk reviews."
python $HOME\.codex\skills\external-advisor\scripts\router.py --execute --prompt "Review this architecture decision."
python $HOME\.codex\skills\external-advisor\scripts\router.py --execute --agent-sanitized-workspace always --prompt "Review this architecture decision through a sanitized copy."
python $HOME\.codex\skills\external-advisor\scripts\router.py --prompt-only --prompt "Review this with prompt-only critique."
```

For ChatGPT web Intelligence/thinking choices, set the private ChatGPT field explicitly with `ADVISOR_THINKING_EFFORT` or `--thinking-effort`. Examples: `extended`, `max`, `pro-extended`, `extra-high`, or `high`. This is separate from `ADVISOR_REASONING_EFFORT`; the advisor maps these values to ChatGPT's private web `thinking_effort` field and defaults normal advisor calls to `max`.

Current ChatGPT private effort values are `min`, `standard`, `extended`, and `max`. The advisor maps friendly names to those values: `low`/`light` -> `min`, `medium` -> `standard`, `high` -> `extended`, and `extra-high`/`xhigh`/`heavy` -> `max`. Do not send stale raw values such as `high` or `xhigh` directly to g4f; ChatGPT can reject them as `Invalid conversation body`. Unknown values fail locally unless `ADVISOR_ALLOW_UNKNOWN_THINKING_EFFORT=true` is set for diagnostics. Normal non-Pro advisor calls are policy-clamped to `gpt-5-6-thinking` with `thinking_effort=max`; explicit weaker efforts such as `none`, `min`, or `standard` and explicit non-Pro model overrides are ignored unless `ADVISOR_ALLOW_NON_DEFAULT_ROUTE=true` is set for deliberate diagnostics.

Pro Extended should select both the request model slug and the effort field. Use `ADVISOR_THINKING_EFFORT=pro-extended`; the scripts currently request the detected ChatGPT web Pro slug `gpt-5-6-pro` and send `thinking_effort=standard`, matching the current ChatGPT web metadata for Pro. If a normal default model such as `gpt-5-6-thinking` or `gpt-5-5-thinking` is also set, the scripts override it to `gpt-5-6-pro` to avoid silent downgrades. Override the automatic Pro Extended request model with `ADVISOR_PRO_EXTENDED_MODEL` only after verifying a newer ChatGPT web Pro slug; set `ADVISOR_ALLOW_PRO_MODEL_OVERRIDE=true` only for deliberate diagnostics.

`gpt-5-6-thinking` is the normal advisor default, paired with `thinking_effort=max`. The official API model id is `gpt-5.6-sol`, and the local ChatGPT/g4f route accepts dotted aliases such as `gpt-5.6` or `gpt-5.6-sol` and maps them to the ChatGPT-style slug `gpt-5-6-thinking`. The unsafe legacy route is `gpt-5-5` or `gpt-5-5-thinking` with no private effort, `min`, or `standard`, because current ChatGPT metadata can resolve weak/no-thinking routes to weaker models. The scripts now force every non-Pro request back to `gpt-5-6-thinking` with `thinking_effort=max`, even when a Codex session sets `ADVISOR_MODEL=gpt-5-5`, `ADVISOR_MODEL=gpt-4o`, or `ADVISOR_THINKING_EFFORT=none`. Set `ADVISOR_ALLOW_NON_DEFAULT_ROUTE=true` only for deliberate transport diagnostics. The Pro Extended path requests/defaults `gpt-5-6-pro` with `thinking_effort: standard`, matching the current ChatGPT Pro metadata. To request that path from Codex, use `ADVISOR_THINKING_EFFORT=pro-extended` rather than bare `extended`. This repo's setup patch adds the required g4f/OpenaiChat conversation-turn WebSocket handoff path for extended/max turns. If an extended or max call returns empty text, refresh the HAR/session first, then inspect the WebSocket handoff in `OpenaiChat`.

Do not intentionally set `ADVISOR_MODEL=default`. If an older Codex session or inherited shell environment does pass `default`, `advisor.py` ignores that alias and selects the safe configured model for the requested thinking mode. This avoids creating weaker or unpredictable ChatGPT chats while keeping old sessions from failing unnecessarily.

Pro Extended is intended for hard advisor questions, architecture reviews, high-risk debugging, and important strategic decisions, not routine checks. It can run long prompts silently for several minutes before returning. When neither `--timeout` nor `ADVISOR_TIMEOUT` is supplied, every prompt-only wrapper automatically uses timeout `0` for normal `max` and Pro Extended calls alike: prompt acceptance stays bounded, then the foreground process waits for the actual final turn. Explicit positive timeouts remain deliberate operator deadlines. Router, conclave, verifier, before-final critique, and evaluation subprocesses preserve timeout `0` instead of converting it into a short local kill deadline. Prompt-only conclave also does not launch a useless synthesizer when every specialist failed. If a long call appears to fail but a tiny diagnostic works, do not assume the model cannot handle the prompt. First inspect the actual timeout/transport error; rerun only when the turn journal proves another submission is safe.

The WebSocket stream can carry visible live progress such as reasoning status, summaries, recaps, and metadata. It does not expose private hidden chain-of-thought. If the OpenAI-compatible response returns duplicated, tail-fragment, or empty content after a normal or Pro/extended turn, `advisor.py` prefers the synced ChatGPT transcript for the exact latest prompt when available. Recovery is anchored after the message that preceded the submitted turn, so an identical older prompt cannot supply a stale answer. For empty or suspiciously corrupted turns, it fetches `backend-api/conversation/<id>` after the main stream has ended, then recovers the latest final assistant message after the latest user turn into the local transcript/state. An ordinary first-turn stream failure before a conversation id is returned cannot be correlated safely and therefore fails closed without blind resubmission. Checkpointed repo-aware runs add a unique completion marker and bound ChatGPT Project id, so `advisor_agent.py --resume-run-dir ...` can later discover exactly one matching conversation with GET-only requests; no match or multiple matches remains fail-closed. Repo-aware calls with timeout `0` use a bounded `ADVISOR_FINAL_FETCH_ACCEPTANCE_TIMEOUT` (180 seconds by default) to observe the prompt or active stream, then wait without an arbitrary total or inactive-poll deadline. A positive `--timeout` or `ADVISOR_FINAL_FETCH_TIMEOUT` imposes a total operator deadline; only bounded calls use `ADVISOR_FINAL_FETCH_MAX_POLLS` as an additional inactive check. `ADVISOR_FINAL_FETCH_POLL_SECONDS` controls the low-rate delay, and idempotent remote reads retry bounded transient network failures. Do not disable sync for normal calls, because transcript recovery is the reliable path around g4f transport-body corruption.

Repo-aware ChatGPT agent turns can outlive g4f's initial response stream while ChatGPT continues calling MCP tools. Visible activity messages may each have `status=finished_successfully`, but they are not final when the remote message has `end_turn=false`. Current final messages require `end_turn=true`; for legacy payloads that omit the field, `advisor.py` also confirms the conversation is not still streaming before accepting final-looking text. It keeps the same local process blocked, checks the remote `stream_status` at a low rate, and waits for the final end-of-turn response before saving or returning. This internal wait does not make additional model calls and does not require Codex to poll the shell repeatedly. Repo-aware wrappers pass timeout `0` by default; set a positive timeout only when a bounded operator deadline is required.

For foreground repo-aware calls, the wrappers automatically watch the matching private DevSpace project log for safe live observability. They print only fixed activity lines to stderr: whitelisted tool names, success/failure, bounded duration, a low-rate local heartbeat after 30 seconds of silence, and final-response arrival. They never print MCP arguments, paths, command text, file contents, raw errors, tokens, conversation ids, or private reasoning. This monitor tails a local file and does not poll ChatGPT or consume model credits. Disable it with `--no-live-activity` or `ADVISOR_LIVE_ACTIVITY=false`; change the heartbeat with `ADVISOR_LIVE_ACTIVITY_HEARTBEAT_SECONDS`. Because the log is shared by simultaneous calls, it is observability only. `advisor_agent.py` separately fetches the exact completed ChatGPT conversation graph and fails closed unless that role proves a successful workspace open plus bounded read/search activity with no disallowed tool.

For Pro Extended, `advisor.py` also checks synced ChatGPT metadata after the response. A valid current browser Pro turn can report `model_slug`/`default_model_slug: gpt-5-6-pro` and `thinking_effort: standard`; do not treat a `resolved_model_slug` field alone as a downgrade. For Pro requests, verify the Pro request fields plus the expected private effort. For non-Pro persistent ChatGPT-backed advisor calls, `advisor.py` rejects known downgraded resolved models by default. Currently `ADVISOR_REJECT_RESOLVED_MODEL_SLUGS` defaults to `gpt-5-3-mini`. Set `ADVISOR_ALLOW_RESOLVED_MODEL_DOWNGRADE=true` only for deliberate transport diagnostics.

When Codex runs a foreground Pro Extended advisor command, do not repeatedly poll the shell session and do not send user-facing "still running" updates every few seconds. Let the default unlimited wrapper wait quietly for the process to return. If the execution environment requires polling an active shell session, use long waits of several minutes and report only completion or an actual error. The open WebSocket/HTTP request is already the wait mechanism; extra Codex-side status polling wastes attention and tokens.

For long or important advisor calls, pass `--save` to write the advisor answer to a task-specific file and read that file, the automatic latest-response file, or the synced `transcript.md` before concluding the answer was truncated. By default `advisor.py` writes `.codex-advisor/latest-response.md`; when a ChatGPT Project binding moves state under `.codex-advisor/projects/<g-p-id>/`, it also writes the project-scoped `latest-response.md` there. If `ADVISOR_STATE_PATH` is set it writes `latest-response.md` beside that state file; if `ADVISOR_RESPONSE_PATH` is set it writes exactly there. The CLI reports saved latest-response path(s) on stderr. If the OpenAI-compatible response body is duplicated, empty, or only a tail fragment but the synced ChatGPT transcript contains the latest final answer for the same prompt, `advisor.py` recovers the clean text from the transcript and reports that on stderr. If transcript recovery is disabled by `ADVISOR_TEMPORARY=true`, `ADVISOR_PERSIST_CONVERSATION=false`, or `ADVISOR_SYNC_REMOTE=false`, suspicious output fails closed and must be rerun explicitly with persistent sync; the wrapper never auto-submits a second turn. This latest-response file is a convenience artifact and can be overwritten by concurrent advisor runs, so use `--save` for task-specific evidence. Codex terminal output can be display-truncated to the tail of a long answer; seeing only final punctuation in the terminal is not enough evidence that the advisor returned only punctuation.

For important Pro Extended advisor/conclave calls, prefer foreground execution unless the user explicitly wants a detached job. If detached execution is needed, use `scripts/advisor_background.py` instead of ad hoc `nohup ... & echo $!` shell snippets. The launcher creates a unique run directory with `meta.json`, `status.json`, `heartbeat.json`, `response.md`, `stderr.log`, and `monitor.log`.

Background Pro Extended example:

```bash
ADVISOR_THINKING_EFFORT=pro-extended \
ADVISOR_MAX_OUTPUT_TOKENS=1800 \
python3 ~/.codex/skills/external-advisor/scripts/advisor_background.py -- \
  --context-file docs/experiment_reports/review_prompt.md \
  --prompt "Review the attached prompt and produce the requested critique." \
  --thinking-effort pro-extended \
  --timeout 600
```

Before reporting that Pro Extended failed in a background run, inspect `status.json`, child process existence, response byte count, `stderr.log`, `monitor.log`, prompt path validity, and the recorded command in `meta.json`. Missing or inconsistent metadata means the background wrapper is inconclusive. Empty logs plus no response file are not enough evidence that ChatGPT failed.

Bounded conclave for harder tasks:

```powershell
$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:8080/v1"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\conclave.py --mode architecture --prompt "Evaluate this architecture decision: ..."
```

Automatic router:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\router.py --prompt "Should this project use a verifier loop or keep manual checks?"
```

Context pack:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\context_pack.py --prompt "Review this plan" --draft "Current plan..." --file README.md
```

Context packs block common sensitive paths such as `.env`, HAR/cookie/auth files, key material, and advisor state. Git full diffs are limited to non-sensitive changed files and redacted before being written. Use `--allow-outside-project` only when the file is intentionally outside the project and contains no secrets.

Create or bind current directory to a ChatGPT Project:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\project_bind.py --create --name "my-project"
python $HOME\.codex\skills\external-advisor\scripts\project_bind.py --url "https://chatgpt.com/g/g-p-.../project" --name "my-project"
python $HOME\.codex\skills\external-advisor\scripts\project_migrate.py --url "https://chatgpt.com/g/g-p-.../project" --name "my-project" --archive-root
```

Before-final critique:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\critique_final.py --prompt "Original user request" --draft "Draft answer to critique"
```

Evidence-backed verifier loop:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\verifier_loop.py --prompt "Verify this patch" --draft "Patch summary" --command "python -m py_compile codex-skill\external-advisor\scripts\verifier_loop.py"
```

The verifier command runner parses argv and runs without a shell. Shell snippets, command substitution, destructive git operations, and arbitrary `python -c` payloads are rejected by default.

Searchable memory:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\memory_manager.py init
python $HOME\.codex\skills\external-advisor\scripts\memory_manager.py record-outcome --task "Architecture decision" --advisor-mode "conclave" --accepted-advice "Keep role memories separate" --outcome "Implemented and tests passed" --useful true --status accepted --confidence 0.8
python $HOME\.codex\skills\external-advisor\scripts\memory_manager.py summary
```

Evaluation harness:

```powershell
python $HOME\.codex\skills\external-advisor\scripts\eval_harness.py --dry-run --limit-per-category 1 --strategy all
```

`conclave.py` runs role-specific advisor calls and writes:

```text
.codex-advisor/
  roles/
    planner/text/conversation.json
    planner/json/conversation.json
    critic/text/conversation.json
    critic/json/conversation.json
  conclave-runs/
  context-packs/
  verifier-runs/
  project-profile.md
  decisions.json
  advisor-lessons.md
  open-questions.md
  outcomes.json
  evaluations/
  memory-summary.md
  latest-evaluation.md
  latest-conclave.md
  latest-context-pack.md
  latest-verifier-loop.md
```

When `router.py --execute` uses an advisor path, it automatically builds a context pack and passes the Markdown pack to conclave/verifier routes. Use `--no-context-pack` only when the prompt is already self-contained or contains sensitive material that should not be packaged.

Decision memory includes age, source, confidence, accepted/rejected/superseded status, contradictions, and superseded decision links. Treat old, low-confidence, contradicted, or superseded memory as weak context.

Available modes: `general`, `architecture`, `strategy`, `code-review`, `security`, `model-choice`, and `verification`.

`conclave.py` uses readable text by default so persistent online ChatGPT advisor chats remain useful to read. Use `--machine-json` or `--output-format json` only when Codex needs strict parsing, validation, or internal automation. Text and JSON role memories are stored separately so structured runs do not contaminate readable advisor chats. In JSON mode, Codex can inspect parsed fields such as `recommendation`, `confidence`, `risks`, `evidence`, `next_actions`, `verification.commands`, and `escalate`.

Saved conclave JSON also includes `ranking.role_rankings`. The deterministic ranking compares role advice by confidence, evidence count, risk severity, actionability, and user-intent conflict signals. Use it to decide which advisor points deserve more weight; do not blindly follow the top score.

The `verification` mode runs only the verifier role and is useful after Codex has a draft plan or patch. Treat plain verifier output as a checklist of evidence to gather, not proof by itself. For stronger verification, use `verifier_loop.py` so the verifier sees actual command output before Codex relies on the result.

Use `scripts/validate_conclave.py` to validate the latest saved machine-JSON run before relying on parsed JSON fields.

Prompt-only `conclave.py` remains serial by default for compatibility. Repo-aware `agent_conclave.py` is parallel by default and may launch up to five local specialist subprocesses together. The remote FIFO admits two ChatGPT turns at once by default, so excess roles wait without polling; each admitted role receives a disposable g4f process. Use `--serial` when deliberate role ordering is preferable. `G4F_MAX_TRANSIENT_WORKERS` (default `32`) is only an emergency local process ceiling and does not override remote admission safety.

Repo-aware roles and synthesis are checkpointed before submission. If Codex, the terminal, or the laptop stops after ChatGPT accepted a turn, the online work may still finish. Resume the same run with `python3 ~/.codex/skills/external-advisor/scripts/agent_conclave.py --resume-run .codex-advisor/agent-conclave-runs/<run-directory>`, or one standalone role with `advisor_agent.py --resume-run-dir <run-directory>`. Recovery reads the bound Project/conversation with GET-only requests, verifies the unique marker and exact MCP evidence, and never replays a turn whose submission began. Only a role whose journal proves submission never started may be launched on resume. Synthesis checkpoints are keyed to the exact successful-role reports, so a later recovered role cannot reuse an older partial synthesis. A per-run lock prevents two Codex sessions from reconciling the same conclave concurrently.

Prompt-only advisor lanes support Windows. Sanitized repo-aware generation currently requires POSIX descriptor-relative no-follow traversal and fails closed when that primitive is unavailable.

Official OpenAI Responses API:

```powershell
$env:ADVISOR_PROVIDER = "openai"
$env:OPENAI_API_KEY = "sk-..."
$env:ADVISOR_MODEL = "gpt-5.6-sol"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\advisor.py --prompt "Review this draft answer: ..."
```

Read `references/g4f.md` when the user specifically asks about using the local `gpt4free` checkout.
