---
name: external-advisor
description: Use when Codex should improve advice, plans, explanations, or important final answers by asking an authorized external reasoning model for second-pass critique through the bundled advisor script. Also use when the user asks to consult ChatGPT Pro, OpenAI API, a local OpenAI-compatible endpoint, or gpt4free/g4f for guidance before answering.
---

# External Advisor

Use this skill as a second-pass critique layer. It does not replace Codex's judgment and it must not be used to bypass access controls or expose private credentials.

## Decision

Use the advisor when the answer is high impact, strategic, ambiguous, user-facing, or when the user explicitly asks for ChatGPT/OpenAI/g4f guidance.

Skip the advisor for routine code edits, simple terminal answers, fast status updates, or when it would require sending secrets, private keys, proprietary data, or unrelated user files to an external model.

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

