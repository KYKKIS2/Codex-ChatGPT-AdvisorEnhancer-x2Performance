# gpt4free / g4f Notes

The local `gpt4free` checkout exposes an OpenAI-compatible API that the advisor script can call.
Setup pins the upstream checkout by default and applies the shared advisor runtime patch for Project binding, `thinking_effort`, and Pro Extended WebSocket handoff.

Start it from this repository with:

```powershell
.\start-g4f.ps1
```

The HAR file must be placed in:

```text
vendor\gpt4free\har_and_cookies
```

The starter keeps one control endpoint on `8080` and defaults to transient mode. `advisor.py` discovers the private machine-wide manifest and serializes calls to the same saved ChatGPT conversation. A separate FIFO admits at most two remote ChatGPT turns by default (`ADVISOR_REMOTE_MAX_CONCURRENCY=2`) and staggers starts by two seconds. Each admitted call receives one isolated g4f process, which is terminated after the call; dead caller processes are reaped automatically. Idempotent remote reads use bounded `Retry-After`-aware backoff. A non-idempotent turn-submission POST is attempted once and never retried automatically; HTTP 429 records a shared cooldown and temporarily reduces later remote admission to one. Keep callers pointed at the base URL rather than selecting transient ports directly. `G4F_MAX_TRANSIENT_WORKERS` defaults to `32` but is only an emergency local process ceiling. Use `G4F_WORKER_MODE=fixed G4F_WORKERS=2` only for compatibility diagnostics.

Inspect or stop the supervisor with `python3 ~/.codex/skills/external-advisor/scripts/g4f_pool.py status` and `python3 ~/.codex/skills/external-advisor/scripts/g4f_pool.py stop`.

Recommended local settings:

```powershell
$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:8080/v1"
$env:ADVISOR_MODEL = "gpt-5-6-thinking"
$env:ADVISOR_REASONING_EFFORT = "high"
```

Conversation persistence:

- Default: `.codex-advisor/conversation.json` in the current working directory.
- Set `ADVISOR_TEMPORARY=true` for throwaway chats.
- Set `ADVISOR_CONVERSATION_KEY` only when multiple advisor chats are needed in the same folder.

Boundary:

- Do not commit or print HAR/cookie contents.
- Do not rely on `OPENAI_API_KEY` for local compatible mode; set `ADVISOR_API_KEY` only if the compatible endpoint requires a token.
- Do not assume local `g4f` behavior exactly matches official OpenAI API behavior.
- Do not post directly to `/v1/chat/completions`; that bypasses transient-worker cleanup and conversation locks.
- Do not raise remote concurrency to chase throughput. The ChatGPT web backend does not publish the same limit headers or account-tier table as the official API, so use the wrapper's conservative FIFO and backoff defaults.
- Treat external output as advisory critique and verify important claims.
