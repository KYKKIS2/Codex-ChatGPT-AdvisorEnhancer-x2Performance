param(
    [int]$Port = 8088,
    [string[]]$ProjectDir = @(),
    [switch]$NoRegisterCwd
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$G4f = Join-Path $Root "vendor\gpt4free"
$Py = Join-Path $G4f ".venv\Scripts\python.exe"
$Gui = Join-Path $Root "codex-skill\external-advisor\scripts\advisor_gui.py"
$RuntimePatch = Join-Path $Root "patches\apply_gpt4free_runtime_patch.py"

if (-not (Test-Path (Join-Path $G4f "g4f")) -or -not (Test-Path $Py)) {
    & (Join-Path $Root "setup.ps1")
}

& python $RuntimePatch $G4f | Out-Null
if ($LASTEXITCODE -ne 0) { throw "gpt4free advisor runtime patch failed." }

$GuiArgs = @($Gui, "serve", "--port", $Port)
foreach ($Path in $ProjectDir) {
    $GuiArgs += @("--project-dir", $Path)
}
if ($NoRegisterCwd) {
    $GuiArgs += "--no-register-cwd"
}

& $Py @GuiArgs
exit $LASTEXITCODE
