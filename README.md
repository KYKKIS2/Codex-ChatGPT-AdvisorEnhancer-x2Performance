# Codex ChatGPT Advisor Enhancer

![Codex Advisor Enhancer banner](assets/codex-advisor-banner.png)

Codex executes the work. ChatGPT supplies a bounded second reasoning pass.

This repository installs a Codex skill that can consult a project-scoped ChatGPT
advisor for architecture, strategy, planning, risk analysis, code review, and
other decisions where a second model can improve the result. It keeps routine
coding fast, preserves advisor conversations per project, supports independent
specialist conclaves, and defaults repository analysis to verified read-only
inspection of a sanitized DevSpace MCP snapshot when that connector is ready.

This is an experimental, unofficial integration. It depends on ChatGPT web
session data, private web behavior, `gpt4free`, and a sanitized HAR export.
Those interfaces can change without notice.

## Project Aim

The project is built around a clear separation of responsibilities:

```text
Codex:
  inspect the real checkout
  edit files
  run commands and tests
  verify facts
  make the final decision

ChatGPT advisor:
  challenge plans
  compare alternatives
  identify risks
  perform deeper review
  suggest evidence and next actions
```

Advisor output is guidance, not proof. Codex must verify repository facts,
runtime behavior, commands, metrics, and file references locally.

## Four Advisor Lanes

| Lane | Script | Repository access | Best use |
| --- | --- | --- | --- |
| Prompt-only advisor | `advisor.py` | Only supplied text/files | One focused critique or judgment pass |
| Prompt-only conclave | `conclave.py` | Only supplied text/files | Independent planner, critic, security, verifier, and synthesis passes |
| Repo-aware advisor | `advisor_agent.py` | Read-only sanitized DevSpace snapshot | One evidence-backed repository review |
| Repo-aware conclave | `agent_conclave.py` | Separate read-only agent conversations | Hard architecture, security, and broad code audits |

`router.py` chooses among these lanes, no-advisor, and verifier workflows.
When a verified repo-aware connector is ready, repository analysis prefers an
agent lane. Local tunnel health alone is insufficient: one direct repo-aware
call must first prove the current ChatGPT attachment with exact MCP evidence.
`--prompt-only` forces the original context-only behavior.

## Main Capabilities

- Installs the `external-advisor` Codex skill and the other bundled skills in
  `codex-skill/`.
- Runs a local OpenAI-compatible `g4f` supervisor with disposable per-call workers.
- Defaults normal calls to `gpt-5-6-thinking` with
  `thinking_effort=max`.
- Supports the current Pro route through
  `ADVISOR_THINKING_EFFORT=pro-extended`.
- Persists ChatGPT conversation state and transcript files per local project.
- Binds local directories to existing ChatGPT Projects.
- Recovers complete answers from the synchronized ChatGPT conversation when
  the compatible response contains an empty body, duplicate text, or a tail
  fragment.
- Serializes calls to the same saved conversation.
- Gives each admitted call its own isolated g4f process and closes it after the
  call, while a separate FIFO remote-safety queue limits ChatGPT traffic.
- Builds redacted context packs and evidence-backed verifier loops.
- Creates content-hashed, read-only sanitized snapshots for repo-aware review,
  verifies reused generations, and records safe source Git provenance.
- Verifies the exact ChatGPT conversation used one expected workspace and made
  successful read/search calls before accepting an agent answer.
- Checkpoints each repo-aware role and synthesis before submission so an
  interrupted conclave can recover completed online work without replaying an
  ambiguous ChatGPT turn.
- Shows sanitized live tool activity without exposing arguments, paths,
  contents, raw errors, credentials, conversation IDs, or private reasoning.

## Repository Layout

```text
.
|-- codex-skill/
|   `-- external-advisor/
|       |-- SKILL.md
|       `-- scripts/
|-- patches/
|-- tests/
|-- setup.sh
|-- setup.ps1
|-- start-g4f.sh
|-- start-g4f.ps1
|-- AGENTS.md
`-- BUNDLED_SKILLS.md
```

Runtime and authentication state is local and ignored:

```text
vendor/gpt4free/
.codex-advisor/
~/.codex/advisor-runtime/
~/.codex/advisor-agent/
```

## Requirements

Required:

- Git
- Python 3 and `venv`
- Node.js and npm
- A ChatGPT account and a fresh sanitized HAR export

Repo-aware agent mode also needs:

- `cloudflared` for the default managed quick tunnel, or another public HTTPS
  tunnel URL
- ChatGPT Developer Mode / custom MCP app support on the account

The setup scripts install the pinned DevSpace package when `devspace` is not
already available. They do not modify ChatGPT account settings.

## Install

### Ubuntu / Linux

```bash
git clone https://github.com/KYKKIS2/Codex-ChatGPT-AdvisorEnhancer-x2Performance.git
cd Codex-ChatGPT-AdvisorEnhancer-x2Performance
chmod +x setup.sh start-g4f.sh tests/*.sh
./setup.sh
```

Install `cloudflared` before using the automatic repo-aware tunnel.

### Windows

```powershell
git clone https://github.com/KYKKIS2/Codex-ChatGPT-AdvisorEnhancer-x2Performance.git
Set-Location Codex-ChatGPT-AdvisorEnhancer-x2Performance
.\setup.ps1
```

Setup:

1. Clones the pinned `gpt4free` revision into `vendor/gpt4free`.
2. Creates its local virtual environment and installs dependencies.
3. Applies and verifies the ChatGPT Project, model-routing, WebSocket, recovery,
   and runtime patches.
4. Installs or verifies pinned DevSpace `1.0.4` and applies the read-only tool
   mode patch.
5. Installs each folder under `codex-skill/` into
   `${CODEX_HOME:-~/.codex}/skills`.
6. Preserves the previous installed skill in
   `~/.codex/skill-backups/<timestamp>/` before replacement.
7. Creates the private HAR directory with owner-only permissions where the
   platform supports them.

The setup refuses an unverified or unexpectedly modified `vendor/gpt4free`
checkout. `ADVISOR_ALLOW_UNVERIFIED_VENDOR=true` is a diagnostic escape hatch,
not a normal installation setting.

Restart Codex after installation so new sessions discover the installed skills.
Already-running sessions may continue using their previously loaded skill text.

## Add The HAR

In ChatGPT:

1. Open browser developer tools.
2. Select **Network**.
3. Enable recording and **Preserve log**.
4. Make a small normal ChatGPT request. For model/agent transport diagnostics,
   capture the exact model or MCP tool flow being diagnosed.
5. Select the requests and choose **Export HAR (sanitized)**.

Put the exported file in:

```text
vendor/gpt4free/har_and_cookies/
```

The filename is not important. The HAR is authentication material:

- never commit it
- never paste it into a prompt
- never print its contents
- refresh it when ChatGPT authentication or model metadata becomes stale

A HAR refresh does not attach an MCP connector to an existing ChatGPT
conversation. App attachment is conversation state in ChatGPT.

## Start The Local Advisor Supervisor

Linux:

```bash
./start-g4f.sh
```

Windows:

```powershell
.\start-g4f.ps1
```

The default supervisor keeps one control/health endpoint on port `8080`.
Control and transient workers bind explicitly to `127.0.0.1`; local health and
completion requests disable inherited proxies and reject redirects.
Each wrapped advisor call receives a new isolated g4f process on a private
transient port; that process is terminated as soon as the call exits. Advisor
callers always use:

```text
http://127.0.0.1:8080/v1
```

The wrapper leases transient workers through a private machine-wide
coordinator. Calls to the same saved conversation remain serialized. Separate
state files that reference the same conversation share the conversation lock;
on a first turn, the active state-path lock is upgraded to the returned
conversation id before that id is persisted. Unknown first turns temporarily
lease every remote slot, preventing a known conversation alias from holding the
new id lock while the first turn waits to upgrade it. Independently of the local process
ceiling, a machine-wide FIFO admits at most two remote
ChatGPT turns by default. The running supervisor records that capacity as the
machine-wide authority, so conflicting caller environments cannot silently
raise it. The oldest waiter keeps its FIFO ticket while its disposable worker
starts; the two-second pacing gate runs only when that worker is ready and
immediately before the remote turn submission. Excess calls wait without
polling ChatGPT. The local emergency ceiling remains 32, but it is not a
recommended remote concurrency target.

If ChatGPT returns HTTP `429`, the wrapper records a machine-wide cooldown and
temporarily reduces new remote admission to one turn. Idempotent conversation,
status, and evidence `GET` requests honor numeric `Retry-After` when present and
otherwise use jittered exponential backoff capped at 60 seconds. A
non-idempotent turn-submission `POST` is attempted exactly once per wrapper
invocation. Every POST-side error fails closed; recovery may only perform
anchored, idempotent transcript/status reads because a second submission after
ambiguous acceptance can create a duplicate ChatGPT branch. This follows
[OpenAI's rate-limit guidance](https://developers.openai.com/api/docs/guides/rate-limits),
although the private ChatGPT web backend does not publish an exact concurrency
limit. Do not post directly to `/v1/chat/completions` or select a transient port
manually; direct calls bypass admission control, lifecycle cleanup, conversation
locks, model checks, and transcript recovery.

Pool management:

```bash
python3 ~/.codex/skills/external-advisor/scripts/g4f_pool.py status
python3 ~/.codex/skills/external-advisor/scripts/g4f_pool.py stop
```

Useful diagnostics:

```bash
G4F_MAX_TRANSIENT_WORKERS=16 ./start-g4f.sh
python3 ~/.codex/skills/external-advisor/scripts/g4f_pool.py stop
ADVISOR_REMOTE_MAX_CONCURRENCY=1 ./start-g4f.sh
G4F_WORKER_MODE=fixed G4F_WORKERS=2 ./start-g4f.sh
G4F_PORT=8180 ./start-g4f.sh
G4F_DEBUG=true ./start-g4f.sh
```

Keep `ADVISOR_REMOTE_MAX_CONCURRENCY=2` as the normal maximum. Lower it to `1`
when the account is already being throttled. Capacity is captured when the
supervisor starts, so stop and restart that supervisor after changing it;
per-caller overrides are intentionally ignored while the supervisor is live.
Raising it should be a deliberate diagnostic after stable low-rate operation,
not the default for conclaves.

## Basic Advisor Usage

Normal prompt-only call:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor.py \
  --prompt "Review this architecture decision and identify the main risks."
```

Prompt-only conclave:

```bash
python3 ~/.codex/skills/external-advisor/scripts/conclave.py \
  --mode architecture \
  --parallel \
  --prompt "Compare the architecture options and challenge the current plan."
```

Automatic router:

```bash
python3 ~/.codex/skills/external-advisor/scripts/router.py \
  --execute \
  --prompt "What is the safest architecture for this change?"
```

The router does not block or specially route a prompt merely because it
discusses authentication, tokens, privacy, security, or other topic words.
Normal task-shape signals choose the lane; use an explicit forced route when a
security-specialist conclave is actually wanted. Prompt-only calls transmit the
prompt, generated context-pack data, and explicitly selected context verbatim,
including selected paths outside the project, because Codex controls that bounded payload. Set
`ADVISOR_PROMPT_PROTECTION=true` only when legacy value redaction and protected
prompt-context filtering are deliberately wanted. Repo-aware calls keep their
mandatory sanitized-workspace, secret-scan, and denied-path controls because
the remote agent can discover files beyond the explicit prompt.

Force prompt-only behavior:

```bash
python3 ~/.codex/skills/external-advisor/scripts/router.py \
  --execute \
  --prompt-only \
  --prompt "Critique this summary without repository access."
```

Codex sessions can also be told directly:

```text
Use the external advisor for this decision.
```

## Model Routing

Normal calls are policy-clamped to:

```text
model: gpt-5-6-thinking
thinking_effort: max
```

Legacy or weak overrides such as `default`, `gpt-4o`, `gpt-5-5`,
`gpt-5-5-thinking`, or `thinking_effort=none` are ignored for normal calls.
Use `ADVISOR_ALLOW_NON_DEFAULT_ROUTE=true` only for a deliberate transport
diagnostic.

Hard questions can request the Pro route:

```bash
ADVISOR_THINKING_EFFORT=pro-extended \
python3 ~/.codex/skills/external-advisor/scripts/advisor.py \
  --prompt "Perform a deep architecture and failure-mode review."
```

When neither `--timeout` nor `ADVISOR_TIMEOUT` is supplied, every prompt-only
lane uses `--timeout 0`: prompt acceptance remains bounded, but completion may
take as long as ChatGPT needs. This applies to normal `max`, Pro Extended,
router, conclave, verifier, before-final critique, and evaluation calls. An
explicit positive timeout remains an operator deadline, and each wrapper layer
preserves the selected policy instead of introducing its own local kill
deadline. A conclave also skips synthesis when no specialist completed
successfully.

The current wrapper maps that request to the detected ChatGPT web Pro model
slug and its required private effort metadata. Pro calls can take several
minutes. Keep the command open and let the WebSocket/HTTP request wait; frequent
Codex-side polling does not make the answer arrive sooner.

## Conversation And Project State

By default, a repository stores local advisor state under:

```text
.codex-advisor/
```

With a ChatGPT Project binding:

```text
.codex-advisor/
|-- project.json
`-- projects/<g-p-id>/
    |-- conversation.json
    |-- transcript.json
    |-- transcript.md
    |-- latest-response.md
    |-- conversations/
    `-- roles/
```

Preserve `.codex-advisor/project.json`. Deleting it can make later sessions
create duplicate ChatGPT Projects. The whole `.codex-advisor/` directory stays
ignored and uncommitted.

Bind an existing ChatGPT Project:

```bash
python3 ~/.codex/skills/external-advisor/scripts/project_bind.py \
  --project-dir . \
  --url "https://chatgpt.com/g/g-p-.../project" \
  --name "My Project"
```

Create and bind a private ChatGPT Project:

```bash
python3 ~/.codex/skills/external-advisor/scripts/project_bind.py \
  --project-dir . \
  --create \
  --name "My Project"
```

Migrate older root state:

```bash
python3 ~/.codex/skills/external-advisor/scripts/project_migrate.py \
  --project-dir . \
  --url "https://chatgpt.com/g/g-p-.../project" \
  --archive-root
```

Use `ADVISOR_CONVERSATION_KEY` only when one directory intentionally needs
separate topic conversations. Calls to the same conversation are serialized;
independent conversations receive separate disposable workers.

Do not set these for normal calls:

```text
ADVISOR_TEMPORARY=true
ADVISOR_PERSIST_CONVERSATION=false
ADVISOR_SYNC_REMOTE=false
```

They disable the persistent transcript path used to recover full answers from
corrupted compatible transport bodies.

## Repo-Aware Agent Mode

Repo-aware mode lets ChatGPT inspect repository evidence through a custom
DevSpace MCP app. It does not give ChatGPT the original checkout through the
normal route.

### Safety Boundary

The default workflow:

1. Scans the source project for sensitive paths and obvious secret patterns.
2. Builds a generation under
   `~/.codex/advisor-agent/workspaces/<project>/generations/<hash>/`.
3. Omits `.git`, `.codex-advisor`, dependency/build/cache directories,
   `.env*`, HAR/cookie/auth files, keys, wallet/seed material, browser profiles,
   symlinks, binary files, archives, databases, and oversized files.
4. Redacts secret-looking text only when a second scan confirms the redacted
   result is clean.
5. Builds the authoritative source plan while holding the per-project
   generation lock, verifies source and target hashes while publishing, then
   repeats the full source-tree and Git-provenance scan before reuse or return.
   Source changes fail closed instead of returning a stale generation.
6. Rejects symlinks and non-regular source entries such as FIFOs, sockets, and
   devices, opens every source path component with no-follow descriptor-relative
   traversal, and performs cleanup without following hostile symlinks.
7. Rechecks every copied path, directory, hash, generated-metadata checksum,
   symlink boundary, and exact mode before reusing an existing generation.
8. Writes a public `SANITIZED_WORKSPACE_MANIFEST.json` with counts, hashes, and
   source Git commit/tree and dirty-state provenance, without omitted or
   redacted source filenames.
9. Stores the complete omission and redaction audit in a mode-`0600` private
   manifest under the project workspace parent, outside the exact MCP root.
10. Makes directories and executable files mode `0500` and other files mode
   `0400`.
11. Starts the patched DevSpace server in `readonly` tool mode on literal
    `127.0.0.1`.
12. Pins DevSpace to one exact current generation through a private atomic
    pointer. Historical generations and staging directories remain outside the
    MCP-readable boundary even though the same connector URL can be reused.

The exposed MCP tool surface contains workspace open, read, grep, glob, and
list operations. Shell, write, edit, and patch tools are not registered.
The patched server verifies the exact runtime registration set, advertises only
checkout mode, rejects worktree/base-ref opens, and rechecks the exact pinned
root on every workspace operation. `--allow-shell` is rejected by the agent
wrappers.

Repo-aware wrappers also require `ADVISOR_PROVIDER=openai-compatible` and a
loopback `ADVISOR_BASE_URL` (`127.0.0.1`, `localhost`, or `::1`). They reject a
remote compatible endpoint before constructing or sending repository-derived
prompts. Loopback requests bypass proxy environment variables, reject HTTP
redirects, and require every resolved address to remain loopback. Prompt-only
advisor lanes may still use an explicitly configured
official or remote provider when appropriate.

This is defense in depth, not a guarantee that heuristic secret detection can
identify every possible sensitive value. It is not a general PII, customer-data,
or legal-classification engine. Obtain the data owner's approval before exposing
repositories with customer records or unusual confidential data. Review the
private manifest locally because its omission list and source filenames can
themselves be sensitive; that detailed list is not exposed through MCP. Codex
still validates findings in the original checkout.

### Start The Connector

From the project to review:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_agent_connect.py \
  serve \
  --project-dir .
```

The helper:

- validates the project and user-level allowed-root config
- refreshes the sanitized snapshot
- verifies the DevSpace read-only patch
- starts DevSpace
- starts a managed Cloudflare quick tunnel when no public URL is configured
- verifies local and public OAuth challenges
- records each managed child before readiness so `status` and `stop` can recover
  an interrupted startup without leaving an untracked process
- prints the exact `https://.../mcp` connector URL

Then, in ChatGPT:

1. Open **Settings -> Apps & Connectors -> Advanced settings**.
2. Enable **Developer Mode**.
3. Create an app/connector with the printed `/mcp` URL.
4. Keep the DevSpace Owner password private.
5. Open a new chat, enable that app, and perform one bounded read-only test.

The helper cannot edit ChatGPT account settings or attach the app to a chat.
Connector attachment is conversation-specific and can become stale after a
tunnel or app replacement.

Refreshing repository content does not require a new MCP URL. Before each
repo-aware turn the wrapper builds and verifies the latest sanitized generation,
then atomically moves the running connector's exact-root pointer to that one
generation. A URL change is needed only when the tunnel/app itself changes.

A fresh or rotated connector starts with `agent_mode_ready: no`. After adding
the URL in ChatGPT, run one direct `advisor_agent.py` call as shown below. A
successful fail-closed MCP turn records the attachment against that unchanged
connector and changes `agent_mode_ready` to `yes`. Until then, `router.py`
automatically stays on the prompt-only lanes. An explicit `--force-route
agent-mode` remains available for the initial diagnostic call.

Lifecycle:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_agent_connect.py \
  status --project-dir .

python3 ~/.codex/skills/external-advisor/scripts/advisor_agent_connect.py \
  stop --project-dir .
```

If the tunnel URL changes, update the ChatGPT app and validate it in a new chat.
`status` distinguishes `starting`, `connector-ready`, `failed`, `stopped`, and
stale runtime state; `stop` is safe to run after an interrupted `serve` attempt.

### Run One Repo-Aware Review

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_agent.py \
  --project-dir . \
  --prompt "Inspect the relevant implementation and audit this design."
```

Acceptance is fail-closed. The wrapper checks the exact current ChatGPT turn for:

- one real attempted and successful `open_workspace` in the private DevSpace log
- the expected sanitized generation path
- the returned workspace ID and root
- at least one successful read, grep, glob, or list call
- open-before-inspection ordering
- consistent workspace ID reuse
- no denied, escaping, shell, or mutation tool attempts
- a final end-of-turn answer containing the unique completion marker

The ChatGPT conversation graph is the primary role evidence. Some current
ChatGPT agent payloads retain a successful read/search result but omit that
tool's request node. In that case the wrapper accepts the result only when the
private DevSpace log has a matching successful tool record under the unique
workspace ID returned by the same conversation's `open_workspace`. The shared
log window alone is never accepted as role evidence, so unrelated concurrent
calls cannot satisfy the check.

The graph can also retain an unmatched failed `open_workspace` request even
when DevSpace executed exactly one successful open. That graph-only artifact is
accepted only when the private workspace-attributed log proves exactly one real
open and all normal path, ordering, workspace-ID, and denied-tool checks pass.

### Run A Repo-Aware Conclave

```bash
python3 ~/.codex/skills/external-advisor/scripts/agent_conclave.py \
  --project-dir . \
  --mode architecture \
  --roles architect,planner,critic,security,verifier \
  --parallel \
  --max-workers 5 \
  --prompt "Audit the architecture, security, implementation risks, and tests."
```

Each specialist uses an isolated ChatGPT conversation and must independently
prove its repository reads. The local process may launch up to five role
subprocesses together, but the machine-wide remote FIFO admits only two ChatGPT
turns at once by default. Waiting roles do not create remote traffic. Every
admitted role gets its own disposable g4f process. Synthesis is prompt-only and
receives the verified specialist reports; it does not claim additional
repository inspection.

Each role receives a private request checkpoint and turn journal before its
non-idempotent submission. If Codex or the terminal stops after ChatGPT accepted
the work, the online agents may continue to completion. Resume the same run
later:

```bash
python3 ~/.codex/skills/external-advisor/scripts/agent_conclave.py \
  --resume-run .codex-advisor/agent-conclave-runs/<run-directory>
```

Resume first performs only project-conversation and transcript `GET` requests.
It matches the unique checkpointed prompt, verifies the final marker and exact
DevSpace evidence, and recovers the saved report. A role is submitted after
resume only when its local journal proves submission never began. A submitted
turn that is still running or not yet discoverable remains `remote-pending` and
is never replayed automatically. The prompt-only synthesis uses the same
checkpoint and GET-only recovery rule. One per-run lock prevents two local
Codex sessions from reconciling or submitting the same interrupted run at once.
Synthesis checkpoints are keyed to the exact successful-role reports, so a
role recovered after an earlier partial synthesis triggers a new synthesis
instead of silently reusing the incomplete one.

One interrupted standalone agent can be reconciled directly:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_agent.py \
  --resume-run-dir .codex-advisor/agent-runs/<run-directory>
```

Repo-aware calls wait for the real final ChatGPT turn by default:

```text
--queue-timeout 0
--timeout 0
```

`0` means no arbitrary completion or inactive-poll deadline after the submitted
turn is observed. Prompt or stream acceptance must first appear within
`ADVISOR_FINAL_FETCH_ACCEPTANCE_TIMEOUT` (180 seconds by default), so a missing
or definitely unaccepted turn cannot retain locks forever. Idempotent remote
reads retry bounded transient network failures, while transport failures that
cannot be correlated to a conversation, invalid model routes, denied MCP
activity, dead callers, and supervisor shutdown still fail closed. Set positive
values only when an operator deliberately needs bounded execution.

Normal transcript recovery is anchored to the message that preceded the submitted
turn, so repeating an identical prompt cannot select an older answer. If the
very first turn is accepted remotely but its local stream stops before any
conversation id is returned, an interrupted repo-aware run uses its unique
checkpoint marker to discover exactly one matching conversation in the bound
ChatGPT Project. No match or multiple matches fail closed and never trigger a
blind resubmission.

A failed run updates `latest-agent-conclave-attempt.md` but does not overwrite
the last successful `latest-agent-conclave.md`.

## Live Activity

Foreground repo-aware calls emit fixed, sanitized stderr events for:

- allowed tool completion or failure
- bounded tool duration
- low-rate heartbeat after inactivity
- final-response arrival

The monitor tails a private local log. It does not poll ChatGPT and does not
make additional model calls. Disable it with:

```text
--no-live-activity
ADVISOR_LIVE_ACTIVITY=false
```

## Context Packs And Verification

Build a bounded context pack:

```bash
python3 ~/.codex/skills/external-advisor/scripts/context_pack.py \
  --prompt "Review this plan." \
  --draft "Current plan..." \
  --file README.md
```

Context packs include selected files, compact Git evidence, failures,
constraints, and advisor memory summaries. Common sensitive paths are refused,
and prompt, draft, failure, diff, and command output text is redacted before
persistence or advisor use.

Run an evidence-backed verifier loop:

```bash
python3 ~/.codex/skills/external-advisor/scripts/verifier_loop.py \
  --prompt "Verify this patch." \
  --draft "Patch summary..." \
  --command "python3 -m py_compile codex-skill/external-advisor/scripts/router.py" \
  --command "git diff --check"
```

The verifier command runner uses `shell=False` and a constrained argv allowlist.
Command substitution, destructive Git commands, arbitrary `python -c`, and
other unsafe forms are rejected unless an explicit diagnostic escape hatch is
used.

## Saved Artifacts

Common local artifacts:

```text
.codex-advisor/
|-- latest-response.md
|-- transcript.md
|-- context-packs/
|-- conclave-runs/
|-- verifier-runs/
|-- agent-runs/
|-- agent-conclave-runs/
|-- latest-agent-conclave-attempt.md
`-- latest-agent-conclave.md
```

Use `--save <path>` for task-specific advisor output. Shared
`latest-response.md` files are convenience pointers and can be overwritten by
another call.

## Tests

All repository test entrypoints live under `tests/`.

Fast Linux regression suite:

```bash
./tests/test-router.sh
./tests/test-context-pack.sh
./tests/test-verifier-loop.sh
./tests/test-advisor-transport-recovery.sh
python3 ./tests/test-prompt-transport.py
python3 ./tests/test-prompt-conclave-orchestration.py
./tests/test-advisor-live-activity.sh
./tests/test-advisor-concurrency.sh
./tests/test-security-regressions.sh
./tests/test-agent-mode.sh
./tests/test-agent-conclave.sh
./tests/test-memory.sh
./tests/test-ranking.sh
./tests/test-eval-harness.sh
```

Live endpoint checks:

```bash
./tests/test-advisor.sh
./tests/test-conclave.sh
```

Windows:

```powershell
.\tests\test-router.ps1
.\tests\test-context-pack.ps1
.\tests\test-verifier-loop.ps1
.\tests\test-advisor-concurrency.ps1
.\tests\test-memory.ps1
.\tests\test-ranking.ps1
.\tests\test-eval-harness.ps1
.\tests\test-advisor.ps1
.\tests\test-conclave.ps1
```

The repo-aware regression coverage is currently shell-based.

## Troubleshooting

### `Connection refused` on port 8080

Start or inspect the managed supervisor:

```bash
./start-g4f.sh
python3 ~/.codex/skills/external-advisor/scripts/g4f_pool.py status
```

### `Error in message stream`, HTTP 500, or HTTP 422

1. Confirm calls use the wrappers, not direct HTTP.
2. Check supervisor health.
3. Refresh the sanitized HAR.
4. Retry with a fresh conversation key only when the saved conversation itself
   is invalid.
5. Do not fall back to a weaker model as a normal recovery strategy.

The coordinator does not blindly replay ambiguous failures because a request
may already have created a remote turn.

### Empty, duplicated, or truncated-looking answer

Keep conversation persistence and remote sync enabled. Read the saved response
and matching transcript. The wrapper performs exact-prompt recovery until the
configured deadline; repo-aware calls have no deadline by default. It fails
closed when it cannot confirm a complete final answer.

### Repo-aware connector is not ready

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_agent_connect.py \
  status --project-dir .
```

Read only the private connector status/log artifacts needed for diagnosis.
Restart `serve` after setup updates so the running DevSpace process loads the
read-only patch. A process started by an older version is intentionally treated
as stale.

### ChatGPT app works in a new chat but not an old one

That is usually conversation-specific app state. Re-enable the app if the
surface allows it, or bind the advisor to a new verified chat. Replacing a HAR
does not attach tools to an existing conversation.

### Duplicate ChatGPT Projects

Preserve the local `.codex-advisor/project.json` binding. Rebind the directory
to the intended Project instead of deleting all advisor state.

## Security And Privacy

Never commit or send:

- HAR files or cookies
- access or refresh tokens
- private keys or wallet material
- `.env` values
- browser profiles
- customer or unrelated private data
- `.codex-advisor` transcripts and conversation state
- DevSpace Owner passwords

The repository ignores common secret and runtime paths, including:

```text
vendor/
.codex-advisor/
.devspace/
.cloudflared/
*.har
*.cookie.json
*.cookies.json
auth_*.json
.env
.env.*
*.pem
*.key
*.p12
*.pfx
*.log
```

Before release, inspect the exact staged commit surface and scan it for secrets.
Whole-repository scans can include historical examples or generated vendor
content; the staged diff is the authoritative release scope.

## Bundled Skills

Setup installs every directory under `codex-skill/`. See
[BUNDLED_SKILLS.md](BUNDLED_SKILLS.md) for the inventory, sources, and
attribution. See [AGENTS.md](AGENTS.md) for repository-specific routing
guidance.

## Limitations

- ChatGPT private web endpoints and model slugs are not stable public APIs.
- HAR-backed authentication can expire.
- `gpt4free` transport behavior can differ from official OpenAI APIs.
- A public MCP URL must remain live while ChatGPT uses the connector.
- ChatGPT app attachment and availability can vary by conversation and account
  surface.
- Secret detection is heuristic; sanitized snapshots intentionally prefer
  omission over completeness and are not a general PII classifier.
- Interrupted recovery requires valid ChatGPT authentication, the original
  project binding, and exactly one conversation matching the checkpointed
  prompt. Otherwise the run remains pending and requires operator diagnosis.
- Read-only agent review does not replace Codex testing or local verification.
- Prompt-only advisor lanes support Windows, but sanitized repo-aware generation
  currently requires POSIX descriptor-relative no-follow traversal and fails
  closed on platforms that cannot provide it.

The durable idea is independent of the prototype transport:

```text
local execution by Codex
+ project-scoped external reasoning
+ bounded multi-role critique
+ verified read-only repository evidence
+ local final verification
```
