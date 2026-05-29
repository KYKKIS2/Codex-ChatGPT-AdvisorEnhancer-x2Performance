# Codex ChatGPT Advisor Enhancer

Portable setup for giving Codex a second-pass ChatGPT/g4f advisor.

The repo installs a Codex skill named `external-advisor`, clones `gpt4free`, applies the small compatibility patch needed for current ChatGPT HAR captures, and provides helper scripts to start and test the local OpenAI-compatible API.

`gpt4free` is not committed into this repository. `setup.ps1` / `setup.sh` download it into `vendor/gpt4free` when you install. This keeps this repo small, avoids copying upstream code into your repo, and makes it easier to update `gpt4free` later.

The HAR file is intentionally not included. It is sensitive authentication material.

## What This Gives You

- Codex can ask a local ChatGPT-backed advisor before finalizing important answers.
- The advisor uses `g4f` through `http://localhost:8080/v1`.
- Project-local conversation binding is enabled by default:

```text
<your project>\.codex-advisor\conversation.json
```

That means new Codex sessions in the same working directory continue the same ChatGPT advisor chat.

## Install

### Windows

PowerShell:

```powershell
.\setup.ps1
```

### Ubuntu/Linux

Install basic prerequisites first:

```bash
sudo apt update
sudo apt install -y git python3 python3-pip python3-venv
```

Then run:

```bash
chmod +x setup.sh start-g4f.sh test-advisor.sh
./setup.sh
```

The installer will:

- clone `https://github.com/xtekky/gpt4free` into `vendor/gpt4free`
- install Python dependencies
- apply `patches/gpt4free-advisor.patch`
- install the Codex skill to `%USERPROFILE%\.codex\skills\external-advisor` on Windows or `${CODEX_HOME:-$HOME/.codex}/skills/external-advisor` on Linux
- write `advisor-config.json` into the installed skill so Codex knows the exact `start-g4f` script path
- create `vendor/gpt4free/har_and_cookies`

## Add Your HAR

Put your ChatGPT HAR file here:

```text
vendor\gpt4free\har_and_cookies\
```

On Ubuntu/Linux the same path is:

```text
vendor/gpt4free/har_and_cookies/
```

Do not commit or share it.

## Start The Local API

PowerShell:

```powershell
.\start-g4f.ps1
```

PowerShell requires the `.\` prefix for scripts in the current folder. `start-g4f.ps1` without `.\` will not run.

If `vendor/gpt4free` is missing, `start-g4f.ps1` / `start-g4f.sh` will run setup automatically before starting the API.

Ubuntu/Linux:

```bash
./start-g4f.sh
```

Default model:

```text
gpt-5-thinking
```

You can override it:

```powershell
.\start-g4f.ps1 -Model gpt-5-5-pro
```

```bash
./start-g4f.sh gpt-5-5-pro
```

Note: `gpt-5-thinking` returned final API text in testing. `gpt-5-5-pro` triggered ChatGPT thinking in the browser but may return only a thinking marker through the API.

## Test The Advisor

With `start-g4f.ps1` still running in another terminal:

```powershell
.\test-advisor.ps1
```

```bash
./test-advisor.sh
```

## Use From Codex

After setup, restart Codex so it discovers the skill. Then say:

```text
Use the external advisor for this answer.
```

The skill is also configured to trigger automatically for broad judgment questions rather than routine coding execution, for example:

```text
I am not sure which direction to take. Can you advise me on the best approach and tradeoffs?
```

Codex should treat architecture, what-to-do-next, strategy, planning, tool/model choice, tradeoff, design direction, and high-impact recommendation questions as a good time to ask the advisor before answering. It should not use the advisor for ordinary implementation/debugging that Codex can handle directly. If you want to guarantee it for a specific message, explicitly say `Use the external advisor`.

If the local advisor API is not already running, the skill tells Codex to start `start-g4f.ps1` automatically in the background, then wait for `http://localhost:8080/v1/models` before asking the advisor.

The skill uses these defaults:

```powershell
$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://localhost:8080/v1"
$env:ADVISOR_MODEL = "gpt-5-thinking"
$env:ADVISOR_REASONING_EFFORT = "high"
```

For one persistent advisor chat per project, do not set `ADVISOR_CONVERSATION_KEY`. The conversation state is saved in the working directory.

For multiple separate advisor chats inside the same project, set:

```powershell
$env:ADVISOR_CONVERSATION_KEY = "my-topic"
```

## Fresh Advisor Chat

Delete:

```text
.codex-advisor\conversation.json
```

or set a different `ADVISOR_CONVERSATION_KEY`.

## Safety

- This setup depends on your own local HAR/session and `g4f`.
- Do not commit `vendor/gpt4free/har_and_cookies`.
- Do not use this to bypass access controls or share private session material.
- Treat advisor output as critique, not ground truth.
