param(
    [string]$Model = "gpt-5-5-thinking",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Advisor = Join-Path $Root "codex-skill\external-advisor\scripts\advisor.py"

$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://localhost:$Port/v1"
$env:ADVISOR_MODEL = $Model
$env:ADVISOR_REASONING_EFFORT = "high"
$env:ADVISOR_MAX_OUTPUT_TOKENS = "500"

python $Advisor --prompt "Smoke test. Reply with ADVISOR_SETUP_OK and one short sentence."
