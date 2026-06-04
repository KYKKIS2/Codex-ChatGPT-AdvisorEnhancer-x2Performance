$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ContextPack = Join-Path $Root "codex-skill\external-advisor\scripts\context_pack.py"
$Latest = Join-Path $Root ".codex-advisor\latest-context-pack.json"

python $ContextPack `
    --prompt "Smoke test context pack generation." `
    --draft "Plan: include README and git context." `
    --file "README.md" `
    --constraint "Keep advisor context compact."
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path $Latest)) {
    throw "Expected context pack output was not written: $Latest"
}

$data = Get-Content -Raw $Latest | ConvertFrom-Json
if ($data.task -ne "Smoke test context pack generation.") {
    throw "Unexpected task in context pack."
}
if (-not $data.relevant_files -or $data.relevant_files[0].path -ne "README.md") {
    throw "Expected README.md in relevant_files."
}
if (-not $data.git) {
    throw "Expected git context in context pack."
}

Write-Host "Context pack smoke test passed."
