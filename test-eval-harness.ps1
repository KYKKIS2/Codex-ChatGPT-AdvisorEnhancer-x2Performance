$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$EvalHarness = Join-Path $Root "codex-skill\external-advisor\scripts\eval_harness.py"
$Latest = Join-Path $Root ".codex-advisor\latest-evaluation.json"

python $EvalHarness --dry-run --limit-per-category 1 --strategy all
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path $Latest)) {
    throw "Expected evaluation output was not written: $Latest"
}

$data = Get-Content -Raw $Latest | ConvertFrom-Json
if ($data.results.Count -ne 16) {
    throw "Expected 16 results for 4 categories x 4 strategies, got $($data.results.Count)."
}
if ($data.summary.Count -ne 4) {
    throw "Expected 4 strategy summaries."
}
if (-not ($data.categories -contains "architecture") -or -not ($data.categories -contains "model-choice")) {
    throw "Expected benchmark categories."
}

Write-Host "Evaluation harness smoke test passed."
