$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Memory = Join-Path $Root "codex-skill\external-advisor\scripts\memory_manager.py"
$Project = Join-Path $env:TEMP ("advisor-memory-test-" + [guid]::NewGuid())
$AdvisorDir = Join-Path $Project ".codex-advisor"

try {
    New-Item -ItemType Directory -Force -Path $Project | Out-Null

    python $Memory --project-dir $Project init
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $decision = python $Memory --project-dir $Project record-decision `
        --id "memory-smoke-decision" `
        --decision "Use evidence-backed verifier loops for failed tests." `
        --rationale "Verifier advice should be connected to command output." `
        --source "test-memory.ps1" `
        --confidence 0.9 `
        --status "accepted" `
        --tag "verifier"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $outcome = python $Memory --project-dir $Project record-outcome `
        --id "memory-smoke-outcome" `
        --task "Smoke test memory manager." `
        --advisor-mode "verifier-loop" `
        --accepted-advice "Run a harmless command." `
        --rejected-advice "Do not run unsafe shell commands." `
        --outcome "Memory files were written." `
        --useful "true" `
        --source "test-memory.ps1" `
        --confidence 0.8 `
        --status "accepted"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python $Memory --project-dir $Project summary
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $decisions = Get-Content -Raw (Join-Path $AdvisorDir "decisions.json") | ConvertFrom-Json
    $outcomes = Get-Content -Raw (Join-Path $AdvisorDir "outcomes.json") | ConvertFrom-Json
    if (-not ($decisions | Where-Object { $_.id -eq "memory-smoke-decision" })) {
        throw "Expected smoke decision in decisions.json."
    }
    if (-not ($outcomes | Where-Object { $_.id -eq "memory-smoke-outcome" })) {
        throw "Expected smoke outcome in outcomes.json."
    }
    if (-not (Test-Path (Join-Path $AdvisorDir "memory-summary.md"))) {
        throw "Expected memory-summary.md."
    }

    Write-Host "Memory manager smoke test passed."
}
finally {
    Remove-Item -Recurse -Force -LiteralPath $Project -ErrorAction SilentlyContinue
}
