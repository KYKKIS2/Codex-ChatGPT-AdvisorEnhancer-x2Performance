param(
    [string]$Model = "gpt-5-5-thinking",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Conclave = Join-Path $Root "codex-skill\external-advisor\scripts\conclave.py"

$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:ADVISOR_MODEL = $Model
$env:ADVISOR_REASONING_EFFORT = "high"
$env:ADVISOR_MAX_OUTPUT_TOKENS = "700"

python $Conclave --mode strategy --roles planner,critic --no-synthesis --no-sync --prompt "Smoke test. Briefly assess whether a conclave layer should stay bounded and role-based."
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python $Conclave --mode verification --machine-json --no-synthesis --no-sync --prompt "Smoke test. Return verification checks for the conclave setup."
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python (Join-Path $Root "codex-skill\external-advisor\scripts\validate_conclave.py")
exit $LASTEXITCODE
