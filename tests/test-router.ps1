$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Router = Join-Path $Root "codex-skill\external-advisor\scripts\router.py"
$Project = Join-Path $env:TEMP ("advisor-router-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $Project | Out-Null

function Assert-Route {
    param(
        [string]$Expected,
        [string[]]$RouteArgs,
        [string]$ExpectedCommandKind = ""
    )
    $json = python $Router --project-dir $Project --json @RouteArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $data = $json | ConvertFrom-Json
    $route = $data.route
    if ($route -ne $Expected) {
        Write-Error "Expected route '$Expected' but got '$route' for args: $RouteArgs"
        exit 1
    }
    if ($ExpectedCommandKind -and $data.command_kind -ne $ExpectedCommandKind) {
        Write-Error "Expected command kind '$ExpectedCommandKind' but got '$($data.command_kind)' for args: $RouteArgs"
        exit 1
    }
    Write-Host "Route OK: $Expected"
}

function Assert-RouteMode {
    param(
        [string]$ExpectedRoute,
        [string]$ExpectedMode,
        [string[]]$RouteArgs
    )
    $json = python $Router --project-dir $Project --json @RouteArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $data = $json | ConvertFrom-Json
    if ($data.route -ne $ExpectedRoute -or $data.mode -ne $ExpectedMode) {
        Write-Error "Expected route/mode '$ExpectedRoute/$ExpectedMode' but got '$($data.route)/$($data.mode)' for args: $RouteArgs"
        exit 1
    }
    if (($data.reasons -join " ") -like "*security/privacy topic terms*") {
        Write-Error "Prompt topic words unexpectedly selected a security route: $($data.reasons -join ' ')"
        exit 1
    }
    Write-Host "Route mode OK: $ExpectedRoute/$ExpectedMode"
}

try {
    Assert-Route "no-advisor" @("--prompt", "fix typo in README")
    Assert-Route "single-advisor" @("--prompt", "Decide the architecture for advisor memory")
    Assert-Route "single-advisor" @("--prompt", "Review security and privacy risks for token storage")
    Assert-Route "single-advisor" @("--allow-sensitive-advisor", "--prompt", "Review security and privacy risks for token storage")
    Assert-Route "single-advisor" @("--prompt", "Prepare-goal planning review for a Shopify theme using the owner's authoritative annotated PDF requirements.")
    Assert-Route "no-advisor" @("--prompt", "Give a concise recommendation for a world-class homepage.")
    Assert-Route "verifier" @("--failed-tests", "--prompt", "pytest failed after the patch") "verifier-loop"
    Assert-Route "conclave" @("--prompt", "Which model or framework should I use for training?")
    Assert-RouteMode "conclave" "model-choice" @("--prompt-only", "--prompt", "Review sequence model training with ordered event tokens and a frozen HGB residual Transformer comparison.")
    Assert-Route "single-advisor" @("--before-final", "--draft", "Draft answer", "--prompt", "Review before final")
    Assert-Route "machine-json-verifier" @("--machine-verify", "--prompt", "Verify this patch") "verifier-loop"
}
finally {
    Remove-Item -Recurse -Force -LiteralPath $Project -ErrorAction SilentlyContinue
}
