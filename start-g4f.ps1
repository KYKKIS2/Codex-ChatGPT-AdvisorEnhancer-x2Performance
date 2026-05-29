param(
    [string]$Model = "",
    [string]$Provider = "OpenaiAccount",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$G4f = Join-Path $Root "vendor\gpt4free"

if (-not (Test-Path (Join-Path $G4f "g4f"))) {
    throw "gpt4free is not installed. Run .\setup.ps1 first."
}

if ([string]::IsNullOrWhiteSpace($Model)) {
    if ([string]::IsNullOrWhiteSpace($env:G4F_MODEL)) {
        $Model = "gpt-5-thinking"
    } else {
        $Model = $env:G4F_MODEL
    }
}

$env:G4F_PROVIDER = $Provider
$env:G4F_MODEL = $Model

Write-Host "Starting g4f API on http://localhost:$Port/v1"
Write-Host "Provider: $Provider"
Write-Host "Model: $Model"

Push-Location $G4f
try {
    python -m g4f api --port $Port --debug
}
finally {
    Pop-Location
}

