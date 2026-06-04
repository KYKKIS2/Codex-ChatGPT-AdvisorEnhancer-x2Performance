$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Router = Join-Path $Root "codex-skill\external-advisor\scripts\router.py"

function Assert-Route {
    param(
        [string]$Expected,
        [string[]]$RouteArgs,
        [string]$ExpectedCommandKind = ""
    )
    $json = python $Router --json @RouteArgs
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

Assert-Route "no-advisor" @("--prompt", "fix typo in README")
Assert-Route "single-advisor" @("--prompt", "Decide the architecture for advisor memory")
Assert-Route "conclave" @("--prompt", "Review security and privacy risks for token storage")
Assert-Route "verifier" @("--failed-tests", "--prompt", "pytest failed after the patch") "verifier-loop"
Assert-Route "conclave" @("--prompt", "Which model or framework should I use for training?")
Assert-Route "single-advisor" @("--before-final", "--draft", "Draft answer", "--prompt", "Review before final")
Assert-Route "machine-json-verifier" @("--machine-verify", "--prompt", "Verify this patch") "verifier-loop"
