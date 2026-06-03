# Codex ChatGPT Advisor Enhancer

![Codex Advisor Enhancer banner](assets/codex-advisor-banner.png)

Give Codex a project-scoped reasoning advisor for the moments where raw coding ability is not enough.

Codex is excellent at reading files, editing code, running tests, and debugging. But many real engineering decisions are not just code edits. They are architecture calls, tradeoffs, planning decisions, model choices, deployment strategy, and "what should I do next?" questions.

This repo prototypes a simple idea:

```text
Codex handles execution.
GPT-5.5 Thinking acts as a second-pass advisor.
Each project keeps its own advisor memory.
```

The result is a Codex workflow that feels sharper on high-impact decisions without slowing down routine implementation work.

## Why This Matters

Most coding-agent failures do not happen because the agent cannot type code. They happen because the agent confidently chooses the wrong direction, misses a constraint, overbuilds, underplans, or fails to compare tradeoffs.

The `external-advisor` skill helps with that layer.

It is designed for questions like:

- Which architecture should I choose?
- What should I build next?
- Is this model/tool/strategy a good direction?
- What are the risks before I deploy or demo this?
- What am I missing in this plan?
- Should I simplify, scale, refactor, or wait?

It is intentionally not meant for every small bug fix. Codex should still handle normal coding/debugging directly.

## What It Does

- Installs a Codex skill named `external-advisor`.
- Starts a local OpenAI-compatible `g4f` API.
- Uses `gpt-5-5-thinking` by default with high reasoning effort.
- Persists one advisor conversation per working directory.
- Syncs the online ChatGPT advisor chat before and after each persistent advisor call.
- Writes local transcript files Codex can inspect later.
- Lets you explicitly say `Use the external advisor` when you want a second opinion.

Project-local memory looks like this:

```text
your-project/
  .codex-advisor/
    conversation.json   # continuation state
    transcript.json     # synced structured transcript
    transcript.md       # readable synced transcript
```

That means:

```text
Codex session for project A <-> ChatGPT advisor chat for project A
Codex session for project B <-> ChatGPT advisor chat for project B
```

You can even write in the same ChatGPT advisor chat online, and the next Codex advisor call will sync that conversation back into the local transcript.

## The Big Idea For Codex

This repository is a prototype, not the ideal production design.

The valuable part is not `g4f` or HAR files. The valuable part is the workflow:

```text
Project-scoped Codex advisor memory
+ stronger reasoning for judgment-heavy questions
+ automatic use only when it matters
```

An official Codex version could replace the local `g4f`/HAR layer with OpenAI-managed model access, authentication, privacy controls, and project memory. That would make this workflow cleaner, safer, and much more reliable.

## Current Prototype Stack

This repo currently uses:

- Codex skill: `external-advisor`
- Local API: `http://localhost:8080/v1`
- Provider: `OpenaiAccount`
- Default model: `gpt-5-5-thinking`
- Reasoning effort: `high`
- Backend bridge: `gpt4free`
- Local transcript sync: `.codex-advisor/transcript.md`

`gpt4free` is not committed into this repository. The setup scripts download it into:

```text
vendor/gpt4free
```

The HAR file is never included. It is sensitive authentication material and must stay local.

## Quick Start

Clone the repo, then run setup.

### Windows

PowerShell:

```powershell
.\setup.ps1
```

### Ubuntu/Linux

Install prerequisites:

```bash
sudo apt update
sudo apt install -y git python3 python3-pip python3-venv
```

Run setup:

```bash
chmod +x setup.sh start-g4f.sh test-advisor.sh
./setup.sh
```

Setup will:

- clone `https://github.com/xtekky/gpt4free` into `vendor/gpt4free`
- install Python dependencies
- apply `patches/gpt4free-advisor.patch`
- install the Codex skill into your Codex skills folder
- write `advisor-config.json` so Codex knows the exact local start script path
- create `vendor/gpt4free/har_and_cookies`

## Add Your HAR

Put your ChatGPT HAR file here:

```text
vendor\gpt4free\har_and_cookies\
```

On Ubuntu/Linux:

```text
vendor/gpt4free/har_and_cookies/
```

Do not commit it. Do not share it. Do not paste it into chats.

## Start The Local API

PowerShell:

```powershell
.\start-g4f.ps1
```

PowerShell requires the `.\` prefix. Running `start-g4f.ps1` without it may fail or open the file as text.

Ubuntu/Linux:

```bash
./start-g4f.sh
```

If `vendor/gpt4free` is missing, the start script will run setup automatically before starting the API.

Default model:

```text
gpt-5-5-thinking
```

Optional Pro test:

```powershell
.\start-g4f.ps1 -Model gpt-5-5-pro
```

```bash
./start-g4f.sh gpt-5-5-pro
```

`gpt-5-5-thinking` has been the most reliable default in testing. `gpt-5-5-pro` can work, but may sometimes return blank or thinking-only API output.

## Test The Advisor

Keep the local API running, then run:

```powershell
.\test-advisor.ps1
```

```bash
./test-advisor.sh
```

Expected behavior: the advisor returns a short `ADVISOR_SETUP_OK` response.

## Use From Codex

After setup, restart Codex so it discovers the skill.

You can force it:

```text
Use the external advisor for this answer.
```

The skill is also designed to trigger automatically for broad judgment questions, for example:

```text
I am not sure which direction to take. Can you advise me on the best approach and tradeoffs?
```

Codex should use the advisor for:

- architecture decisions
- what-to-do-next planning
- strategy and roadmap questions
- tool/model selection
- tradeoff analysis
- design direction
- high-impact recommendations

Codex should skip the advisor for:

- routine code edits
- direct debugging
- simple terminal answers
- low-risk implementation work

## Memory Sync

For one persistent advisor chat per project, do not set `ADVISOR_CONVERSATION_KEY`.

The default behavior is:

```text
Before advisor call:
  sync latest online ChatGPT conversation
  update local transcript

Advisor call:
  send the new question

After advisor call:
  save continuation state
  sync transcript again
```

Files written locally:

```text
.codex-advisor\conversation.json
.codex-advisor\transcript.json
.codex-advisor\transcript.md
```

To skip remote transcript sync for one call:

```powershell
$env:ADVISOR_SYNC_REMOTE = "false"
```

For multiple separate advisor chats inside the same project:

```powershell
$env:ADVISOR_CONVERSATION_KEY = "my-topic"
```

## Fresh Advisor Chat

Delete:

```text
.codex-advisor\conversation.json
```

or set a different `ADVISOR_CONVERSATION_KEY`.

## Why This Can Improve Codex

This does not make Codex magically smarter at every task. It improves the workflow by adding a second reasoning pass exactly where a second pass matters.

Codex remains the executor:

```text
read repo -> edit files -> run commands -> verify work
```

The advisor improves the decision layer:

```text
architecture -> tradeoffs -> risks -> next steps -> better final answer
```

For complex projects, that separation is powerful. You get fast local execution plus a stronger strategy review before Codex commits to a direction.

## Safety

- This setup depends on your own local HAR/session and `g4f`.
- Do not commit `vendor/gpt4free/har_and_cookies`.
- Do not commit `.codex-advisor`.
- Do not use this to bypass access controls or share private session material.
- Treat advisor output as critique, not ground truth.
- Verify important claims before using them.

## Repository Privacy

Ignored by default:

```text
vendor/
har_and_cookies/
.codex-advisor/
*.har
*.cookie.json
.env
```

The public repo should contain only the skill, scripts, docs, and patch files. Your HAR, cookies, and local advisor transcripts stay on your machine.
