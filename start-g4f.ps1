param(
    [string]$Model = "",
    [string]$Provider = "OpenaiAccount",
    [int]$Port = 8080,
    [switch]$DebugLog
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$G4f = Join-Path $Root "vendor\gpt4free"
$Py = Join-Path $G4f ".venv\Scripts\python.exe"
$Pool = Join-Path $Root "codex-skill\external-advisor\scripts\g4f_pool.py"

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
        $Model = "gpt-5-6-thinking"
    } else {
        $Model = $env:G4F_MODEL
    }
}

$env:G4F_PROVIDER = $Provider
$env:G4F_MODEL = $Model

if (-not (Test-Path $Py)) {
    $Py = "python"
}

$RuntimePatch = Join-Path $Root "patches\apply_gpt4free_runtime_patch.py"
if (Test-Path $RuntimePatch) {
    & python $RuntimePatch $G4f | Out-Null
}

if (-not (Test-Path $Pool)) {
    throw "g4f worker-pool supervisor was not found: $Pool"
}

$Workers = if ($env:G4F_WORKERS) { [int]$env:G4F_WORKERS } else { 2 }
$poolArgs = @(
    $Pool,
    "serve",
    "--python", $Py,
    "--g4f-dir", $G4f,
    "--port", $Port,
    "--workers", $Workers,
    "--model", $Model,
    "--provider", $Provider
)
if ($DebugLog -or $env:G4F_DEBUG -in @("1", "true", "yes", "on")) {
    $poolArgs += "--debug"
}

& $Py @poolArgs
exit $LASTEXITCODE
