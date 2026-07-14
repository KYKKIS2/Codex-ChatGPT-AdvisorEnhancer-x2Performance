param(
    [string]$Model = "gpt-5-6-thinking",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Advisor = Join-Path $Root "codex-skill\external-advisor\scripts\advisor.py"
$Project = Join-Path $env:TEMP ("advisor-live-test-" + [guid]::NewGuid())

$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:ADVISOR_MODEL = $Model
$env:ADVISOR_REASONING_EFFORT = "high"
$env:ADVISOR_MAX_OUTPUT_TOKENS = "500"
$env:ADVISOR_AUTO_CREATE_PROJECT = "false"
$env:ADVISOR_PERSIST_CONVERSATION = "false"
$env:ADVISOR_TEMPORARY = "true"
$env:ADVISOR_SYNC_REMOTE = "false"
$env:ADVISOR_AUTO_RETRY_TAIL_FRAGMENT = "false"

try {
    New-Item -ItemType Directory -Force -Path $Project | Out-Null
    $env:ADVISOR_PROJECT_DIR = $Project
    python $Advisor --prompt "Smoke test. Reply with ADVISOR_SETUP_OK and one short sentence."
    $Code = $LASTEXITCODE
}
finally {
    Remove-Item Env:\ADVISOR_PROJECT_DIR -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force -LiteralPath $Project -ErrorAction SilentlyContinue
}
exit $Code
