param(
    [string]$Model = "gpt-5-5-thinking",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VerifierLoop = Join-Path $Root "codex-skill\external-advisor\scripts\verifier_loop.py"
$Project = Join-Path $env:TEMP ("advisor-verifier-test-" + [guid]::NewGuid())
$Latest = Join-Path $Project ".codex-advisor\latest-verifier-loop.json"

$env:ADVISOR_PROVIDER = "openai-compatible"
$env:ADVISOR_BASE_URL = "http://127.0.0.1:$Port/v1"
$env:ADVISOR_MODEL = $Model
$env:ADVISOR_REASONING_EFFORT = "high"
$env:ADVISOR_MAX_OUTPUT_TOKENS = "700"

try {
    New-Item -ItemType Directory -Force -Path $Project | Out-Null

    python $VerifierLoop `
        --project-dir $Project `
        --dry-run `
        --no-sync `
        --prompt "Smoke test the evidence-backed verifier loop." `
        --draft "Plan: run a harmless local command and ask the verifier to interpret the result." `
        --command "python --version"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if (-not (Test-Path $Latest)) {
        throw "Expected verifier loop output was not written: $Latest"
    }

    $data = Get-Content -Raw $Latest | ConvertFrom-Json
    if (-not $data.command_results -or $data.command_results.Count -lt 1) {
        throw "Expected at least one command result in verifier loop output."
    }
    if ($data.command_results[0].status -ne "completed") {
        throw "Expected command to complete, got: $($data.command_results[0].status)"
    }
    if (-not $data.interpretation.recommendation) {
        throw "Expected verifier interpretation recommendation."
    }

    Write-Host "Verifier loop smoke test passed."
}
finally {
    Remove-Item -Recurse -Force -LiteralPath $Project -ErrorAction SilentlyContinue
}
