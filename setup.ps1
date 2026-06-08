param(
    [string]$Gpt4FreeUrl = "https://github.com/xtekky/gpt4free.git"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor"
$G4f = Join-Path $Vendor "gpt4free"
$Venv = Join-Path $G4f ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Patch = Join-Path $Root "patches\gpt4free-advisor.patch"
$SkillSource = Join-Path $Root "codex-skill\external-advisor"
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$SkillDest = Join-Path $CodexHome "skills\external-advisor"
$SkillConfig = Join-Path $SkillDest "advisor-config.json"

New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

if (-not (Test-Path $G4f)) {
    git clone $Gpt4FreeUrl $G4f
} elseif (-not (Test-Path (Join-Path $G4f ".git"))) {
    Write-Host "Using existing vendor\gpt4free directory without Git metadata."
}

Push-Location $G4f
try {
    if (-not (Test-Path $Py)) {
        python -m venv $Venv
    }
    & $Py -m pip install --upgrade pip
    & $Py -m pip install -r requirements.txt
    & $Py -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv

    $hasTemporary = Select-String -Path "g4f\api\stubs.py" -Pattern "temporary: Optional\[bool\]" -Quiet
    $hasHarFallback = Select-String -Path "g4f\Provider\openai\har_file.py" -Pattern "using generated proof token fallback" -Quiet
    if (-not ($hasTemporary -and $hasHarFallback)) {
        git apply --check --recount $Patch
        git apply --recount $Patch
    } else {
        Write-Host "gpt4free base advisor patch already applied."
    }

    $hasGizmoId = Select-String -Path "g4f\api\stubs.py" -Pattern "gizmo_id: Optional\[str\]" -Quiet
    if (-not $hasGizmoId) {
        $stubsPath = "g4f\api\stubs.py"
        $text = Get-Content -Raw -Path $stubsPath
        $text = $text -replace '    extra_body: Optional\[dict\] = None\r?\n', "    extra_body: Optional[dict] = None`r`n    gizmo_id: Optional[str] = None`r`n    conversation_mode: Optional[dict] = None`r`n"
        Set-Content -Encoding UTF8 -Path $stubsPath -Value $text
        Write-Host "Added gpt4free ChatGPT Project passthrough fields."
    }

    New-Item -ItemType Directory -Force -Path "har_and_cookies" | Out-Null
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $SkillDest | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $SkillSource "*") -Destination $SkillDest
@{
    setup_dir = $Root
    start_g4f = (Join-Path $Root "start-g4f.ps1")
    base_url = "http://127.0.0.1:8080/v1"
    model = "gpt-5-5-thinking"
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $SkillConfig

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next steps:"
Write-Host "1. Put your ChatGPT HAR file in: $G4f\har_and_cookies"
Write-Host "2. Start the local API: .\start-g4f.ps1"
Write-Host "3. Restart Codex so it discovers the external-advisor skill."
