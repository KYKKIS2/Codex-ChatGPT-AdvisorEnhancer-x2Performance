---
name: external-advisor
description: "Use automatically when Codex is asked for broader judgment rather than direct coding execution: architecture decisions, what to do next, strategy, planning, tool or model choice, tradeoff analysis, design direction, high-impact recommendations, or important guidance where a second-pass critique from an external reasoning model would materially improve quality. Do not use for routine implementation/debugging that Codex can handle directly. Also use when the user asks to consult ChatGPT Pro, an external advisor, OpenAI API, a local OpenAI-compatible endpoint, or gpt4free/g4f before answering."
---

# External Advisor

Use this skill as a second-pass critique and verification layer. It does not replace Codex's judgment and it must not be used to bypass access controls or expose private credentials.

For difficult judgment tasks, use the advisor as a bounded conclave rather than a single generic second opinion. Codex remains the orchestrator and final decision maker.

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

Use `scripts/conclave.py` instead of `scripts/advisor.py` when the task would benefit from multiple specialist viewpoints, such as:

- architecture or strategy decisions with meaningful tradeoffs
- plans that should be attacked before implementation
- security/privacy-sensitive design
- model, framework, or tool choices with multiple plausible options
- high-impact recommendations where Codex should compare alternatives before answering
- evidence checks where a verifier should identify commands, tests, inspections, and expected signals

Use `scripts/router.py` when Codex needs to decide the path from the task shape. It chooses among no-advisor, single-advisor, conclave, verifier loop, and machine-json verifier loop.

Use `scripts/context_pack.py` before advisor calls that need repository context. It builds a compact task bundle with the task, draft/plan, selected files, git status/diff, failures, constraints, and existing advisor memory summaries.

Use `scripts/critique_final.py` before sending important user-facing advice. Codex should draft, ask the critic advisor to attack the draft, then revise the final answer itself.

Use `scripts/verifier_loop.py` when the work needs evidence, especially after failed tests or a risky patch. It asks the verifier for a checklist, runs explicit `--command` checks by default, then asks the verifier to interpret the real command output. Use `--run-suggested` only after Codex reviews and accepts running safe advisor-suggested commands.

Use `scripts/memory_manager.py` to initialize searchable advisor memory, record accepted/rejected advice and outcomes, and summarize stale or low-confidence decisions.

Use `scripts/eval_harness.py` to compare Codex-only, single-advisor, conclave, and critic/verifier lanes across architecture, code-review, debugging, and model-choice tasks. Use `--dry-run` for structure checks; live advisor lanes require the local API.

## Workflow

1. Gather the smallest useful context: the user's request, draft answer or plan, and only the files or snippets needed for critique.
2. If using a local OpenAI-compatible server, set `ADVISOR_PROVIDER=openai-compatible`, `ADVISOR_BASE_URL`, and `ADVISOR_MODEL`.
3. Before calling the advisor, check whether `http://127.0.0.1:8080/v1/models` is reachable.
4. If it is not reachable, automatically start the local g4f API before continuing:
   - First read `advisor-config.json` from this skill folder and use its `start_g4f` path if present.
   - If `ADVISOR_SETUP_DIR` is set, check `$env:ADVISOR_SETUP_DIR\start-g4f.ps1`.
   - Otherwise prefer `.\start-g4f.ps1` in the current working directory, then parent directories.
   - The starter script should install `vendor/gpt4free` automatically by running setup if it is missing.
   - Start it in the background with `Start-Process` and `-WindowStyle Hidden`, then wait until `http://127.0.0.1:8080/v1/models` responds.
5. If using official OpenAI, set `ADVISOR_PROVIDER=openai`, `OPENAI_API_KEY`, and optionally `ADVISOR_MODEL` plus `ADVISOR_REASONING_EFFORT`.
6. Run `scripts/advisor.py` with `--prompt` or stdin.
7. For local OpenAI-compatible calls, the script persists the returned `conversation` object by default under `.codex-advisor` in the nearest Git repo root or current working directory. Later Codex sessions in the same repo continue the same ChatGPT advisor chat.
8. Before and after each persistent local advisor call, the script syncs the remote ChatGPT conversation when possible and writes `.codex-advisor/transcript.json` plus `.codex-advisor/transcript.md`. Codex may inspect those files when it needs the advisor chat history.
9. If `.codex-advisor/project.json` exists, advisor calls should pass its normalized `chatgpt_project_id`/`g-p-...` id to g4f so new ChatGPT chats are created inside that ChatGPT Project. Default local state moves under `.codex-advisor/projects/<g-p-id>/`.
10. If no project binding exists, persistent non-temporary local advisor calls auto-create a private ChatGPT Project named from the repo/folder and write `.codex-advisor/project.json`. Set `ADVISOR_AUTO_CREATE_PROJECT=false` to disable this.
11. To create and bind manually, run `scripts/project_bind.py --create --name "my-project"`. To bind an existing Project, run `scripts/project_bind.py --url "https://chatgpt.com/g/g-p-.../project"`, or set `ADVISOR_CHATGPT_PROJECT_URL`/`ADVISOR_CHATGPT_PROJECT_ID`; the advisor persists the normalized Project id into `.codex-advisor/project.json` on use.
12. To migrate old `.codex-advisor/conversation.json` state after pulling updates, run `scripts/project_migrate.py --url "https://chatgpt.com/g/g-p-.../project" --archive-root`, or omit `--url` to infer a Project id from old remote conversation metadata when possible. Use `--create-missing --archive-root` to create a new private Project when nothing can be inferred.
13. Set `ADVISOR_CONVERSATION_KEY` only when multiple advisor chats are needed in the same folder; keyed state is project-scoped under `.codex-advisor/conversations/` or `.codex-advisor/projects/<g-p-id>/conversations/`.
14. Set `ADVISOR_STATE_PATH` when a caller needs an explicit project-local state file, such as `.codex-advisor\roles\critic\conversation.json`.
15. Set `ADVISOR_TEMPORARY=true` for throwaway ChatGPT chats, or `ADVISOR_PERSIST_CONVERSATION=false` to avoid local continuation state.
16. Set `ADVISOR_SYNC_REMOTE=false` to skip transcript sync for a call.
17. For failed tests or evidence-heavy work, prefer `scripts/verifier_loop.py` over plain `conclave.py --mode verification` because it connects verifier advice to actual command output.
18. Treat the result as advisory. Verify facts, reject weak advice, and incorporate only the parts that improve the final answer.

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
$env:ADVISOR_MODEL = "gpt-5-5-thinking"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\advisor.py --prompt "Review this draft answer: ..."
```

For ChatGPT web Intelligence/thinking choices, set the private ChatGPT field explicitly with `ADVISOR_THINKING_EFFORT` or `--thinking-effort`. Examples: `extended`, `pro-extended`, `xhigh`, `extra-high`, `high`, `medium`, or `none`. This is separate from `ADVISOR_REASONING_EFFORT`; the advisor does not infer private ChatGPT values from the OpenAI-compatible reasoning value.

Pro Extended should select both the model slug and the effort field. Use `ADVISOR_THINKING_EFFORT=pro-extended`; when no explicit model is set, the scripts automatically use `gpt-5-pro` and send `thinking_effort=extended`. If a model is explicitly set with `ADVISOR_MODEL` or `--model`, that explicit model wins. Override the automatic Pro Extended model with `ADVISOR_PRO_EXTENDED_MODEL` if ChatGPT changes the private slug.

`extended` alone was observed in local HAR captures with `model: gpt-5-5-thinking`, `thinking_effort: extended`, and `pro_mode_turn_topic_streaming: true`; that is still the base 5.5 Thinking lane in the ChatGPT UI. The working Pro Extended route observed live is `model: gpt-5-pro` plus `thinking_effort: extended`. To request that path from Codex, use `ADVISOR_THINKING_EFFORT=pro-extended` rather than bare `extended`. This repo's setup patch adds the required g4f/OpenaiChat conversation-turn WebSocket handoff path for that mode. If an extended call returns empty text, refresh the HAR/session first, then inspect the WebSocket handoff in `OpenaiChat`.

Pro Extended is intended for hard advisor questions, architecture reviews, high-risk debugging, and important strategic decisions, not routine checks. It can run long prompts silently for several minutes before returning. If a long Pro Extended call appears to fail but a tiny `OK` diagnostic works, do not assume the model cannot handle the prompt. First rerun the exact command in the foreground with the same environment and arguments. If foreground succeeds, classify the earlier failure as background orchestration or logging failure, not Pro Extended failure.

The WebSocket stream can carry visible live progress such as reasoning status, summaries, recaps, and metadata. It does not expose private hidden chain-of-thought. If the OpenAI-compatible response returns empty content after a Pro/extended turn, `advisor.py` attempts to fall back to `backend-api/conversation/<id>` and fetches the latest finished assistant message after the latest user turn into the local transcript/state.

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
$env:ADVISOR_MODEL = "gpt-5-5-thinking"
$env:ADVISOR_REASONING_EFFORT = "high"
$env:ADVISOR_THINKING_EFFORT = "none"
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

Default conclave behavior is serial for reliability with browser-backed local endpoints. Add `--parallel` only when the local endpoint handles concurrent ChatGPT calls reliably.

Official OpenAI Responses API:

```powershell
$env:ADVISOR_PROVIDER = "openai"
$env:OPENAI_API_KEY = "sk-..."
$env:ADVISOR_MODEL = "gpt-5.5"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\advisor.py --prompt "Review this draft answer: ..."
```

Read `references/g4f.md` when the user specifically asks about using the local `gpt4free` checkout.
