param(
    [string]$Gpt4FreeUrl = "https://github.com/xtekky/gpt4free.git"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor"
$G4f = Join-Path $Vendor "gpt4free"
$Venv = Join-Path $G4f ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Patch = Join-Path $Root "patches\gpt4free-advisor.patch"
$SkillsSource = Join-Path $Root "codex-skill"
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$SkillsDest = Join-Path $CodexHome "skills"
$ExternalAdvisorSkillDest = Join-Path $SkillsDest "external-advisor"
$SkillConfig = Join-Path $ExternalAdvisorSkillDest "advisor-config.json"

New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

if (-not (Test-Path $G4f)) {
    git clone $Gpt4FreeUrl $G4f
} elseif (-not (Test-Path (Join-Path $G4f ".git"))) {
    Write-Host "Using existing vendor\gpt4free directory without Git metadata."
}

Push-Location $G4f
try {
    if (-not (Test-Path $Py)) {
        python -m venv $Venv
    }
    & $Py -m pip install --upgrade pip
    & $Py -m pip install -r requirements.txt
    & $Py -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv

    $hasTemporary = Select-String -Path "g4f\api\stubs.py" -Pattern "temporary: Optional\[bool\]" -Quiet
    $hasHarFallback = Select-String -Path "g4f\Provider\openai\har_file.py" -Pattern "using generated proof token fallback" -Quiet
    if (-not ($hasTemporary -and $hasHarFallback)) {
        git apply --check --recount $Patch
        git apply --recount $Patch
    } else {
        Write-Host "gpt4free base advisor patch already applied."
    }

    $hasGizmoId = Select-String -Path "g4f\api\stubs.py" -Pattern "gizmo_id: Optional\[str\]" -Quiet
    if (-not $hasGizmoId) {
        $stubsPath = "g4f\api\stubs.py"
        $text = Get-Content -Raw -Path $stubsPath
        $text = $text -replace '    extra_body: Optional\[dict\] = None\r?\n', "    extra_body: Optional[dict] = None`r`n    gizmo_id: Optional[str] = None`r`n    conversation_mode: Optional[dict] = None`r`n"
        Set-Content -Encoding UTF8 -Path $stubsPath -Value $text
        Write-Host "Added gpt4free ChatGPT Project passthrough fields."
    }

    $hasThinkingEffort = Select-String -Path "g4f\api\stubs.py" -Pattern "thinking_effort: Optional\[str\]" -Quiet
    if (-not $hasThinkingEffort) {
@'
from pathlib import Path

path = Path("g4f/api/stubs.py")
text = path.read_text(encoding="utf-8")
needle = "    reasoning_effort: Optional[Literal[\"low\", \"medium\", \"high\"]] = None\n"
replacement = needle + "    thinking_effort: Optional[str] = None\n"
if needle not in text:
    raise SystemExit("Could not find reasoning_effort field in g4f/api/stubs.py")
path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
'@ | & $Py -
        Write-Host "Added gpt4free ChatGPT thinking_effort passthrough field."
    }

@'
from pathlib import Path

path = Path("g4f/Provider/needs_auth/OpenaiChat.py")
text = path.read_text(encoding="utf-8")
changed = False

needle = "        reasoning_effort: Optional[str] = None,\n        **kwargs\n"
replacement = (
    "        reasoning_effort: Optional[str] = None,\n"
    "        thinking_effort: Optional[str] = None,\n"
    "        **kwargs\n"
)
if "thinking_effort: Optional[str] = None" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat.create_authed reasoning_effort parameter")
    text = text.replace(needle, replacement, 1)
    changed = True

old_derivation = (
    "            if thinking_effort is None and reasoning_effort in {\"low\", \"medium\", \"high\"}:\n"
    "                thinking_effort = reasoning_effort\n"
)
if old_derivation in text:
    text = text.replace(old_derivation, "", 1)
    changed = True

needle = (
    "            system_hints = [\"picture_v2\"] if image_model else []\n"
    "            if reasoning_effort == \"high\":\n"
    "                system_hints.append(\"reason\")\n"
    "            if web_search:\n"
)
replacement = (
    "            system_hints = [\"picture_v2\"] if image_model else []\n"
    "            if reasoning_effort == \"high\" and thinking_effort is None:\n"
    "                system_hints.append(\"reason\")\n"
    "            if web_search:\n"
)
if "if reasoning_effort == \"high\" and thinking_effort is None" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat system_hints block")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                    if temporary:\n"
    "                        data[\"history_and_training_disabled\"] = True\n"
    "                    if conversation.conversation_id is not None and not temporary:\n"
)
replacement = (
    "                    if temporary:\n"
    "                        data[\"history_and_training_disabled\"] = True\n"
    "                    if thinking_effort:\n"
    "                        data[\"thinking_effort\"] = thinking_effort\n"
    "                    if conversation.conversation_id is not None and not temporary:\n"
)
if text.count("data[\"thinking_effort\"] = thinking_effort") < 1:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat prepare payload insertion point")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                if temporary:\n"
    "                    data[\"history_and_training_disabled\"] = True\n"
    "\n"
    "                if conversation.conversation_id is not None and not temporary:\n"
)
replacement = (
    "                if temporary:\n"
    "                    data[\"history_and_training_disabled\"] = True\n"
    "                if thinking_effort:\n"
    "                    data[\"thinking_effort\"] = thinking_effort\n"
    "\n"
    "                if conversation.conversation_id is not None and not temporary:\n"
)
if text.count("data[\"thinking_effort\"] = thinking_effort") < 2:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat conversation payload insertion point")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                async with session.post(\n"
    "                    backend_anon_url\n"
)
replacement = (
    "                turn_topic_id = None\n"
    "                async with session.post(\n"
    "                    backend_anon_url\n"
)
if "turn_topic_id = None" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat conversation POST block")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                    async for line in response.iter_lines():\n"
    "                        pattern = re.compile(r\"file-service://[\\w-]+\")\n"
)
replacement = (
    "                    async for line in response.iter_lines():\n"
    "                        turn_topic_id = turn_topic_id or cls.get_resume_turn_topic_id(line)\n"
    "                        pattern = re.compile(r\"file-service://[\\w-]+\")\n"
)
if "turn_topic_id = turn_topic_id or cls.get_resume_turn_topic_id(line)" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat SSE line loop")
    text = text.replace(needle, replacement, 1)
    changed = True

needle = (
    "                    if buffer:\n"
    "                        yield buffer\n"
    "                if sources.list:\n"
)
replacement = (
    "                    if buffer:\n"
    "                        yield buffer\n"
    "                if turn_topic_id and conversation.finish_reason is None:\n"
    "                    async for chunk in cls.iter_conversation_turn_ws(\n"
    "                        session,\n"
    "                        auth_result,\n"
    "                        turn_topic_id,\n"
    "                        conversation,\n"
    "                        sources,\n"
    "                        references,\n"
    "                        timeout,\n"
    "                    ):\n"
    "                        yield chunk\n"
    "                if sources.list:\n"
)
if "cls.iter_conversation_turn_ws(" not in text:
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat post-SSE insertion point")
    text = text.replace(needle, replacement, 1)
    changed = True

helpers = r'''
    @classmethod
    def get_turn_topic_id(cls, token: str) -> Optional[str]:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        except Exception as e:
            debug.error(f"OpenaiChat: Could not decode turn topic token: {e}")
            return None
        return data.get("turn_topic_id")

    @classmethod
    def get_resume_turn_topic_id(cls, line: bytes) -> Optional[str]:
        if not line.startswith(b"data: "):
            return None
        try:
            data = json.loads(line[6:])
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("type") != "resume_conversation_token":
            return None
        if data.get("kind") != "topic":
            return None
        token = data.get("token")
        return cls.get_turn_topic_id(token) if isinstance(token, str) else None

    @classmethod
    async def iter_conversation_turn_ws(
        cls,
        session,
        auth_result: AuthResult,
        topic_id: str,
        conversation: Conversation,
        sources: OpenAISources,
        references: ContentReferences,
        timeout: Optional[int] = 120,
    ) -> AsyncIterator:
        async with AsyncSession(
            timeout=timeout,
            impersonate="chrome",
            headers=auth_result.headers,
            cookies=auth_result.cookies
        ) as ws_session:
            response = await ws_session.get(
                "https://chatgpt.com/backend-api/celsius/ws/user",
                headers=auth_result.headers,
            )
            response.raise_for_status()
            websocket_url = response.json().get("websocket_url")
            if not websocket_url:
                raise RuntimeError("OpenaiChat: No websocket_url returned for conversation-turn stream")
            if not isinstance(websocket_url, str) or not websocket_url.startswith("wss://ws.chatgpt.com/"):
                raise RuntimeError("OpenaiChat: Unexpected conversation-turn websocket URL")

            wss = await ws_session.ws_connect(websocket_url, timeout=3)
            command_id = 1
            subscribed = False
            try:
                await wss.send_json([
                    {"id": command_id, "command": {"type": "connect", "presence": {"type": "presence", "state": "foreground"}}},
                    {"id": command_id + 1, "command": {"type": "subscribe", "topic_id": topic_id, "offset": "0"}},
                ])
                command_id += 2
                subscribed = True
                started = False
                while not wss.closed:
                    try:
                        frames = await wss.recv_json(timeout=60 if not started else timeout)
                    except Exception:
                        break
                    if not isinstance(frames, list):
                        frames = [frames]
                    for frame in frames:
                        if not isinstance(frame, dict):
                            continue
                        messages = []
                        reply = frame.get("reply")
                        if isinstance(reply, dict):
                            messages.extend(reply.get("catchups") or [])
                        if frame.get("type") == "message":
                            messages.append(frame)
                        for message in messages:
                            if not isinstance(message, dict) or message.get("topic_id") != topic_id:
                                continue
                            payload = message.get("payload", {})
                            if payload.get("type") != "conversation-turn-stream":
                                continue
                            stream_item = payload.get("payload", {})
                            if conversation.conversation_id is not None and stream_item.get("conversation_id") != conversation.conversation_id:
                                continue
                            encoded_item = stream_item.get("encoded_item")
                            if not isinstance(encoded_item, str):
                                continue
                            started = True
                            for encoded_line in encoded_item.splitlines():
                                async for chunk in cls.iter_messages_line(
                                    session,
                                    auth_result,
                                    encoded_line.encode(),
                                    conversation,
                                    sources,
                                    references,
                                ):
                                    yield chunk
                            if "message_stream_complete" in encoded_item or conversation.finish_reason is not None:
                                return
            finally:
                if subscribed and not wss.closed:
                    try:
                        await wss.send_json([
                            {"id": command_id, "command": {"type": "unsubscribe", "topic_id": topic_id}}
                        ])
                    except Exception:
                        pass
                if not wss.closed:
                    await wss.close()

'''
if "def get_resume_turn_topic_id" not in text:
    needle = "    @classmethod\n    async def wait_media(\n"
    if needle not in text:
        raise SystemExit("Could not find OpenaiChat wait_media insertion point")
    text = text.replace(needle, helpers + needle, 1)
    changed = True

if changed:
    path.write_text(text, encoding="utf-8")
    print("Added gpt4free ChatGPT thinking_effort and WebSocket handoff support.")
else:
    print("gpt4free ChatGPT thinking_effort and WebSocket handoff support already applied.")
'@ | & $Py -

    New-Item -ItemType Directory -Force -Path "har_and_cookies" | Out-Null
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $SkillsDest | Out-Null
Get-ChildItem -Directory -Path $SkillsSource | ForEach-Object {
    $dest = Join-Path $SkillsDest $_.Name
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $_.FullName "*") -Destination $dest
    Write-Host "Installed Codex skill: $($_.Name)"
}
@{
    setup_dir = $Root
    start_g4f = (Join-Path $Root "start-g4f.ps1")
    base_url = "http://127.0.0.1:8080/v1"
    model = "gpt-5-5-thinking"
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $SkillConfig

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next steps:"
Write-Host "1. Put your ChatGPT HAR file in: $G4f\har_and_cookies"
Write-Host "2. Start the local API: .\start-g4f.ps1"
Write-Host "3. Restart Codex so it discovers the external-advisor skill."
