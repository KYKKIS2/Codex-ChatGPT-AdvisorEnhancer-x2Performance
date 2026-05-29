param(
    [string]$Gpt4FreeUrl = "https://github.com/xtekky/gpt4free.git"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor"
$G4f = Join-Path $Vendor "gpt4free"
$Patch = Join-Path $Root "patches\gpt4free-advisor.patch"
$SkillSource = Join-Path $Root "codex-skill\external-advisor"
$SkillDest = Join-Path $HOME ".codex\skills\external-advisor"

New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

if (-not (Test-Path (Join-Path $G4f ".git"))) {
    git clone $Gpt4FreeUrl $G4f
}

Push-Location $G4f
try {
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    python -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv

    $hasTemporary = Select-String -Path "g4f\api\stubs.py" -Pattern "temporary: Optional\[bool\]" -Quiet
    $hasHarFallback = Select-String -Path "g4f\Provider\openai\har_file.py" -Pattern "using generated proof token fallback" -Quiet
    if (-not ($hasTemporary -and $hasHarFallback)) {
        git apply $Patch
    } else {
        Write-Host "gpt4free advisor patch already applied."
    }

    New-Item -ItemType Directory -Force -Path "har_and_cookies" | Out-Null
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $SkillDest | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $SkillSource "*") -Destination $SkillDest

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next steps:"
Write-Host "1. Put your ChatGPT HAR file in: $G4f\har_and_cookies"
Write-Host "2. Start the local API: .\start-g4f.ps1"
Write-Host "3. Restart Codex so it discovers the external-advisor skill."

