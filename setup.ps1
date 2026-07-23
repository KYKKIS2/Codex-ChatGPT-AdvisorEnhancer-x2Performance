param(
    [string]$Gpt4FreeUrl = "https://github.com/xtekky/gpt4free.git",
    [string]$Gpt4FreeRef = "883c717437c4d91b68869359ed05b0427f34df65",
    [string]$DevSpaceVersion = "1.0.4"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vendor = Join-Path $Root "vendor"
$G4f = Join-Path $Vendor "gpt4free"
$Venv = Join-Path $G4f ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$RuntimePatch = Join-Path $Root "patches\apply_gpt4free_runtime_patch.py"
$DevSpacePatch = Join-Path $Root "codex-skill\external-advisor\scripts\devspace_readonly_patch.py"
$SkillsSource = Join-Path $Root "codex-skill"
$CodexHome = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) { Join-Path $HOME ".codex" } else { $env:CODEX_HOME }
$SkillsDest = Join-Path $CodexHome "skills"
$ExternalAdvisorSkillDest = Join-Path $SkillsDest "external-advisor"
$SkillConfig = Join-Path $ExternalAdvisorSkillDest "advisor-config.json"
$AllowUnverifiedVendor = $env:ADVISOR_ALLOW_UNVERIFIED_VENDOR -eq "true"

$RequiredFiles = @(
    (Join-Path $Root "start-g4f.ps1"),
    (Join-Path $Root "tests\test-advisor.ps1"),
    (Join-Path $Root "tests\test-conclave.ps1"),
    (Join-Path $Root "tests\test-router.ps1"),
    (Join-Path $Root "tests\test-context-pack.ps1"),
    (Join-Path $Root "tests\test-verifier-loop.ps1"),
    (Join-Path $Root "tests\test-memory.ps1"),
    (Join-Path $Root "tests\test-ranking.ps1"),
    (Join-Path $Root "tests\test-eval-harness.ps1"),
    (Join-Path $Root "tests\test-advisor-concurrency.ps1"),
    (Join-Path $Root "tests\test-advisor-concurrency.py"),
    $RuntimePatch,
    $DevSpacePatch,
    (Join-Path $Root "codex-skill\external-advisor\SKILL.md"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\advisor.py"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\advisor_concurrency.py"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\advisor_safety.py"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\agent_mode.py"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\advisor_agent.py"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\agent_conclave.py"),
    (Join-Path $Root "codex-skill\external-advisor\scripts\router.py")
)
foreach ($RequiredFile in $RequiredFiles) {
    if (-not (Test-Path -Path $RequiredFile -PathType Leaf)) {
        throw "Required setup file is missing: $RequiredFile"
    }
}

New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

if (-not (Test-Path $G4f)) {
    git clone $Gpt4FreeUrl $G4f
    git -C $G4f checkout --detach $Gpt4FreeRef
} elseif (Test-Path (Join-Path $G4f ".git")) {
    $ExpectedVendorChanges = @(
        "g4f/Provider/needs_auth/OpenaiChat.py",
        "g4f/Provider/openai/har_file.py",
        "g4f/Provider/openai/models.py",
        "g4f/api/stubs.py",
        "g4f/providers/any_model_map.py"
    )
    $currentRef = (git -C $G4f rev-parse HEAD).Trim()
    $status = @(git -C $G4f status --porcelain --untracked-files=all)
    $unexpected = @()
    $meaningfulVendorChanges = 0
    foreach ($line in $status) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.Length -lt 4) { continue }
        $changedPath = $line.Substring(3)
        if ($changedPath.Contains(" -> ")) {
            $changedPath = ($changedPath -split " -> ")[-1]
        }
        if ($changedPath -like ".venv/*" -or $changedPath -like "har_and_cookies/*") {
            continue
        }
        $meaningfulVendorChanges += 1
        if ($ExpectedVendorChanges -notcontains $changedPath) {
            $unexpected += $changedPath
        }
    }
    if ($unexpected.Count -gt 0 -and -not $AllowUnverifiedVendor) {
        throw "Refusing vendor\gpt4free with unexpected local changes: $($unexpected -join ', ')"
    }
    if ($currentRef -ne $Gpt4FreeRef -and $meaningfulVendorChanges -gt 0 -and -not $AllowUnverifiedVendor) {
        throw "Refusing dirty vendor\gpt4free at unexpected revision $currentRef; expected $Gpt4FreeRef."
    }
    if ($currentRef -ne $Gpt4FreeRef) {
        git -C $G4f fetch origin
        git -C $G4f checkout --detach $Gpt4FreeRef
    }
    Write-Host "Verified vendor\gpt4free base revision: $Gpt4FreeRef"
} else {
    if (-not $AllowUnverifiedVendor) {
        throw "Refusing existing vendor\gpt4free without Git metadata. Recreate it or set ADVISOR_ALLOW_UNVERIFIED_VENDOR=true for a deliberate diagnostic."
    }
    Write-Warning "Using unverified vendor\gpt4free because ADVISOR_ALLOW_UNVERIFIED_VENDOR=true."
}

Push-Location $G4f
try {
    if (-not (Test-Path $Py)) {
        python -m venv $Venv
    }
    & $Py -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $Py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "gpt4free requirements install failed." }
    & $Py -m pip install python-multipart a2wsgi Brotli pycryptodome python-dotenv
    if ($LASTEXITCODE -ne 0) { throw "gpt4free supplemental dependency install failed." }

    & python $RuntimePatch $G4f
    if ($LASTEXITCODE -ne 0) { throw "gpt4free advisor runtime patch failed." }
    & $Py -m py_compile `
        "g4f\api\stubs.py" `
        "g4f\Provider\openai\har_file.py" `
        "g4f\Provider\openai\models.py" `
        "g4f\Provider\needs_auth\OpenaiChat.py" `
        "g4f\providers\any_model_map.py"
    if ($LASTEXITCODE -ne 0) { throw "gpt4free patched file compile check failed." }

    $HarDirectory = Join-Path $G4f "har_and_cookies"
    New-Item -ItemType Directory -Force -Path $HarDirectory | Out-Null
    try {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $identity,
            "FullControl",
            "ContainerInherit,ObjectInherit",
            "None",
            "Allow"
        )
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule($rule)
        Set-Acl -Path $HarDirectory -AclObject $acl
    } catch {
        Write-Warning "Could not enforce owner-only ACL on ${HarDirectory}: $_"
    }
}
finally {
    Pop-Location
}

$DevSpaceCommand = Get-Command devspace -ErrorAction SilentlyContinue
$InstalledDevSpaceVersion = if ($null -ne $DevSpaceCommand) {
    (& $DevSpaceCommand.Source --version | Select-Object -Last 1).Trim()
} else {
    ""
}
if ($null -eq $DevSpaceCommand -or $InstalledDevSpaceVersion -ne $DevSpaceVersion) {
    if ($null -eq (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "npm is required to install DevSpace $DevSpaceVersion for repo-aware advisor mode."
    }
    npm install --global "@waishnav/devspace@$DevSpaceVersion"
    if ($LASTEXITCODE -ne 0) { throw "DevSpace install failed." }
    $DevSpaceCommand = Get-Command devspace -ErrorAction Stop
}
$InstalledDevSpaceVersion = (& $DevSpaceCommand.Source --version | Select-Object -Last 1).Trim()
if ($InstalledDevSpaceVersion -ne $DevSpaceVersion) {
    throw "DevSpace version verification failed: expected $DevSpaceVersion, got $InstalledDevSpaceVersion."
}
& python $DevSpacePatch --executable $DevSpaceCommand.Source
if ($LASTEXITCODE -ne 0) { throw "DevSpace read-only patch failed." }

New-Item -ItemType Directory -Force -Path $SkillsDest | Out-Null
$BackupRoot = Join-Path $CodexHome ("skill-backups\" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"))
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
$LockPath = Join-Path $SkillsDest ".advisor-skill-install.lock"
$LockStream = [System.IO.File]::Open($LockPath, "OpenOrCreate", "ReadWrite", "None")
try {
    Get-ChildItem -Directory -Path $SkillsSource | ForEach-Object {
        $dest = Join-Path $SkillsDest $_.Name
        $stage = Join-Path $SkillsDest (".$($_.Name).staging." + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        Copy-Item -Recurse -Force -Path (Join-Path $_.FullName "*") -Destination $stage
        if ($_.Name -eq "external-advisor") {
            foreach ($requiredName in @(
                "SKILL.md",
                "scripts\advisor.py",
                "scripts\advisor_concurrency.py",
                "scripts\advisor_safety.py",
                "scripts\router.py",
                "scripts\advisor_agent.py",
                "scripts\agent_conclave.py",
                "scripts\devspace_readonly_patch.py"
            )) {
                if (-not (Test-Path (Join-Path $stage $requiredName))) {
                    Remove-Item -Recurse -Force $stage
                    throw "Staged external-advisor skill is incomplete: $requiredName"
                }
            }
        }
        $backup = $null
        if (Test-Path $dest) {
            $backup = Join-Path $BackupRoot $_.Name
            Move-Item -Path $dest -Destination $backup
        }
        try {
            Move-Item -Path $stage -Destination $dest
        } catch {
            if ($null -ne $backup -and (Test-Path $backup)) {
                Move-Item -Path $backup -Destination $dest
            }
            throw
        }
        Write-Host "Installed Codex skill: $($_.Name)"
        if ($null -ne $backup) {
            Write-Host "Previous skill preserved at: $backup"
        }
    }
}
finally {
    $LockStream.Dispose()
}

@{
    setup_dir = $Root
    start_g4f = (Join-Path $Root "start-g4f.ps1")
    base_url = "http://127.0.0.1:8080/v1"
    model = "gpt-5-6-thinking"
    worker_mode = "transient"
    control_workers = 1
    max_transient_workers = 32
    remote_max_concurrency = 2
    remote_start_interval_seconds = 2
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
