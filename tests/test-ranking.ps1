$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Conclave = Join-Path $Root "codex-skill\external-advisor\scripts\conclave.py"
$Project = Join-Path $env:TEMP ("advisor-ranking-test-" + [guid]::NewGuid())
$Runs = Join-Path $Project ".codex-advisor\conclave-runs"

try {
    New-Item -ItemType Directory -Force -Path $Project | Out-Null

    python $Conclave `
        --project-dir $Project `
        --dry-run `
        --machine-json `
        --no-synthesis `
        --mode "model-choice" `
        --roles "planner,critic" `
        --prompt "Smoke test ranking of advisor outputs."
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $latest = Get-ChildItem $Runs -Filter "*.json" | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $latest) {
        throw "Expected a conclave run JSON file."
    }

    $data = Get-Content -Raw $latest.FullName | ConvertFrom-Json
    if (-not $data.ranking -or -not $data.ranking.role_rankings -or $data.ranking.role_rankings.Count -lt 2) {
        throw "Expected ranking.role_rankings for planner and critic."
    }
    if (-not ($data.ranking.criteria -contains "confidence")) {
        throw "Expected confidence ranking criterion."
    }

    Write-Host "Ranking smoke test passed."
}
finally {
    Remove-Item -Recurse -Force -LiteralPath $Project -ErrorAction SilentlyContinue
}
