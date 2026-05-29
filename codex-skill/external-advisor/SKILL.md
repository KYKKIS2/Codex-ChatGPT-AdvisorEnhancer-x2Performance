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
3. If using official OpenAI, set `ADVISOR_PROVIDER=openai`, `OPENAI_API_KEY`, and optionally `ADVISOR_MODEL` plus `ADVISOR_REASONING_EFFORT`.
4. Run `scripts/advisor.py` with `--prompt` or stdin.
5. For local OpenAI-compatible calls, the script persists the returned `conversation` object by default at `.codex-advisor/conversation.json` in the current working directory. Later Codex sessions in the same folder continue the same ChatGPT advisor chat.
6. Set `ADVISOR_CONVERSATION_KEY` only when multiple advisor chats are needed in the same folder; keyed state is stored under `%USERPROFILE%\.codex\external-advisor`.
7. Set `ADVISOR_TEMPORARY=true` for throwaway ChatGPT chats, or `ADVISOR_PERSIST_CONVERSATION=false` to avoid local continuation state.
8. Treat the result as advisory. Verify facts, reject weak advice, and incorporate only the parts that improve the final answer.

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
