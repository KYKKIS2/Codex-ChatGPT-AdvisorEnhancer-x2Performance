---
name: external-advisor
description: "Use automatically when Codex is asked for broader judgment rather than direct coding execution: architecture decisions, what to do next, strategy, planning, tool or model choice, tradeoff analysis, design direction, high-impact recommendations, or important guidance where a second-pass critique from an external reasoning model would materially improve quality. Do not use for routine implementation/debugging that Codex can handle directly. Also use when the user asks to consult ChatGPT Pro, an external advisor, OpenAI API, a local OpenAI-compatible endpoint, or gpt4free/g4f before answering."
---

# External Advisor

Use this skill as a second-pass critique layer. It does not replace Codex's judgment and it must not be used to bypass access controls or expose private credentials.

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

## Workflow

1. Gather the smallest useful context: the user's request, draft answer or plan, and only the files or snippets needed for critique.
2. If using a local OpenAI-compatible server, set `ADVISOR_PROVIDER=openai-compatible`, `ADVISOR_BASE_URL`, and `ADVISOR_MODEL`.
3. Before calling the advisor, check whether `http://localhost:8080/v1/models` is reachable.
4. If it is not reachable, automatically start the local g4f API before continuing:
   - First read `advisor-config.json` from this skill folder and use its `start_g4f` path if present.
   - If `ADVISOR_SETUP_DIR` is set, check `$env:ADVISOR_SETUP_DIR\start-g4f.ps1`.
   - Otherwise prefer `.\start-g4f.ps1` in the current working directory, then parent directories.
   - The starter script should install `vendor/gpt4free` automatically by running setup if it is missing.
   - Start it in the background with `Start-Process` and `-WindowStyle Hidden`, then wait until `http://localhost:8080/v1/models` responds.
5. If using official OpenAI, set `ADVISOR_PROVIDER=openai`, `OPENAI_API_KEY`, and optionally `ADVISOR_MODEL` plus `ADVISOR_REASONING_EFFORT`.
6. Run `scripts/advisor.py` with `--prompt` or stdin.
7. For local OpenAI-compatible calls, the script persists the returned `conversation` object by default at `.codex-advisor/conversation.json` in the current working directory. Later Codex sessions in the same folder continue the same ChatGPT advisor chat.
8. Before and after each persistent local advisor call, the script syncs the remote ChatGPT conversation when possible and writes `.codex-advisor/transcript.json` plus `.codex-advisor/transcript.md`. Codex may inspect those files when it needs the advisor chat history.
9. Set `ADVISOR_CONVERSATION_KEY` only when multiple advisor chats are needed in the same folder; keyed state is stored under `%USERPROFILE%\.codex\external-advisor`.
10. Set `ADVISOR_TEMPORARY=true` for throwaway ChatGPT chats, or `ADVISOR_PERSIST_CONVERSATION=false` to avoid local continuation state.
11. Set `ADVISOR_SYNC_REMOTE=false` to skip transcript sync for a call.
12. Treat the result as advisory. Verify facts, reject weak advice, and incorporate only the parts that improve the final answer.

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

Then poll `http://localhost:8080/v1/models` for up to 60 seconds before running `scripts/advisor.py`.

## Commands

Local OpenAI-compatible endpoint:

```powershell
$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://localhost:8080/v1"
$env:ADVISOR_MODEL = "gpt-5-thinking"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\advisor.py --prompt "Review this draft answer: ..."
```

Official OpenAI Responses API:

```powershell
$env:ADVISOR_PROVIDER = "openai"
$env:OPENAI_API_KEY = "sk-..."
$env:ADVISOR_MODEL = "gpt-5.5"
$env:ADVISOR_REASONING_EFFORT = "high"
python $HOME\.codex\skills\external-advisor\scripts\advisor.py --prompt "Review this draft answer: ..."
```

Read `references/g4f.md` when the user specifically asks about using the local `gpt4free` checkout.
