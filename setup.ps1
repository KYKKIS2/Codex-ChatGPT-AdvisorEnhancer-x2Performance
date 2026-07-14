param(
    [string]$Gpt4FreeUrl = "https://github.com/xtekky/gpt4free.git",
    [string]$Gpt4FreeRef = "883c717437c4d91b68869359ed05b0427f34df65"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor"
$G4f = Join-Path $Vendor "gpt4free"
$Venv = Join-Path $G4f ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Patch = Join-Path $Root "patches\gpt4free-advisor.patch"
$RuntimePatch = Join-Path $Root "patches\apply_gpt4free_runtime_patch.py"
$SkillsSource = Join-Path $Root "codex-skill"
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$SkillsDest = Join-Path $CodexHome "skills"
$ExternalAdvisorSkillDest = Join-Path $SkillsDest "external-advisor"
$SkillConfig = Join-Path $ExternalAdvisorSkillDest "advisor-config.json"

New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

if (-not (Test-Path $G4f)) {
    git clone $Gpt4FreeUrl $G4f
    git -C $G4f checkout --detach $Gpt4FreeRef
} elseif (Test-Path (Join-Path $G4f ".git")) {
    $status = git -C $G4f status --porcelain
    if ([string]::IsNullOrWhiteSpace(($status -join "`n"))) {
        git -C $G4f fetch origin
        git -C $G4f checkout --detach $Gpt4FreeRef
    } else {
        Write-Host "Using existing patched vendor\gpt4free checkout without resetting local edits."
    }
} else {
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

    & python $RuntimePatch $G4f

    New-Item -ItemType Directory -Force -Path "har_and_cookies" | Out-Null
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $SkillsDest | Out-Null
Get-ChildItem -Directory -Path $SkillsSource | ForEach-Object {
    $dest = Join-Path $SkillsDest $_.Name
    if (Test-Path $dest) {
        Remove-Item -Recurse -Force $dest
    }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $_.FullName "*") -Destination $dest
    Write-Host "Installed Codex skill: $($_.Name)"
}

@{
    setup_dir = $Root
    start_g4f = (Join-Path $Root "start-g4f.ps1")
    base_url = "http://127.0.0.1:8080/v1"
    model = "gpt-5-6-thinking"
    workers = 2
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $SkillConfig

Write-Host ""
Write-Host "Setup complete."
Write-Host "Pinned gpt4free ref: $Gpt4FreeRef"
Write-Host "Next steps:"
Write-Host "1. Put your ChatGPT HAR file in: $G4f\har_and_cookies"
Write-Host "2. Start the local API: .\start-g4f.ps1"
Write-Host "3. For repo-aware ChatGPT agent mode, run from a project:"
Write-Host "   python $HOME\.codex\skills\external-advisor\scripts\advisor_agent_connect.py serve --project-dir ."
Write-Host "   Then paste the printed /mcp URL into ChatGPT Developer Mode."
Write-Host "4. Restart Codex so it discovers the bundled skills."
