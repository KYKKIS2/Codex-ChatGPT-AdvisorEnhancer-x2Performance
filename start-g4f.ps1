param(
    [string]$Model = "",
    [string]$Provider = "OpenaiAccount",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$G4f = Join-Path $Root "vendor\gpt4free"

if (-not (Test-Path (Join-Path $G4f "g4f"))) {
    $Setup = Join-Path $Root "setup.ps1"
    if (-not (Test-Path $Setup)) {
        throw "gpt4free is not installed and setup.ps1 was not found."
    }
    Write-Host "gpt4free is not installed. Running setup.ps1 first..."
    & $Setup
    if (-not (Test-Path (Join-Path $G4f "g4f"))) {
        throw "setup.ps1 completed but gpt4free is still missing at $G4f"
    }
}

if ([string]::IsNullOrWhiteSpace($Model)) {
    if ([string]::IsNullOrWhiteSpace($env:G4F_MODEL)) {
        $Model = "gpt-5-5-thinking"
    } else {
        $Model = $env:G4F_MODEL
    }
}

$env:G4F_PROVIDER = $Provider
$env:G4F_MODEL = $Model

Write-Host "Starting g4f API on http://127.0.0.1:$Port/v1"
Write-Host "Provider: $Provider"
Write-Host "Model: $Model"

Push-Location $G4f
try {
    python -m g4f api --port $Port --debug
}
finally {
    Pop-Location
}
