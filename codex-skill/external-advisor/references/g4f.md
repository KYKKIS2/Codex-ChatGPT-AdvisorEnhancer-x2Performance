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

Recommended local settings:

```powershell
$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:8080/v1"
$env:ADVISOR_MODEL = "gpt-5-5-thinking"
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
- Treat external output as advisory critique and verify important claims.
