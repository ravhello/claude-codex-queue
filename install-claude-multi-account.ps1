param(
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$Git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $Git) {
    $Git = Get-Command git -ErrorAction SilentlyContinue
}
if ($null -eq $Git) {
    throw "Git for Windows is required. Install it from https://git-scm.com/download/win and run this installer again."
}
$Python = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -eq $Python) {
    $Python = Get-Command python.exe -ErrorAction SilentlyContinue
}
if ($null -eq $Python) {
    throw "Python 3 for Windows is required. Install it from https://www.python.org/downloads/windows/ and run this installer again."
}

$Repository = "https://github.com/Zoltak-Dev/ai-multi-instance.git"
$PinnedCommit = "a5fba4a75f00cd680125f079dc47a3c43d0e0d21"
$ExpectedEngineHash = "A041E3ED220D9B58C290F957C7500EE9D6FABA625B992E2878EFD9E8EAB9A3ED"
$ExpectedMainHash = "A0C29F4E5DD2685E295FD268D1BB3929DB33F28E906E8918263E079FE6410261"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$HomeDir = [Environment]::GetFolderPath("UserProfile")
$PreferredState = Join-Path $HomeDir ".claude-codex-queue"
$LegacyState = Join-Path $HomeDir ".claude-vscode-queue"
$PreferredQueue = Join-Path $PreferredState "queue.json"
$LegacyQueue = Join-Path $LegacyState "queue.json"
$StateDir = if ((Test-Path -LiteralPath $LegacyQueue) -and -not (Test-Path -LiteralPath $PreferredQueue)) {
    $LegacyState
} elseif ((Test-Path -LiteralPath $PreferredState) -or -not (Test-Path -LiteralPath $LegacyState)) {
    $PreferredState
} else {
    $LegacyState
}

$Companions = Join-Path $StateDir "companions"
$Manager = Join-Path $Companions "ai-multi-instance"
$Profiles = Join-Path $Manager "ClaudeProfiles"
New-Item -ItemType Directory -Path $Companions -Force | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Manager ".git"))) {
    if (Test-Path -LiteralPath $Manager) {
        $Existing = Get-ChildItem -LiteralPath $Manager -Force -ErrorAction SilentlyContinue
        if ($Existing.Count -gt 0) {
            throw "The existing directory is not a Git clone: $Manager"
        }
    }
    & $Git.Source clone --filter=blob:none $Repository $Manager
    if ($LASTEXITCODE -ne 0) {
        throw "Could not clone the multi-account manager."
    }
} else {
    $Origin = (& $Git.Source -C $Manager remote get-url origin | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $Origin -notmatch "Zoltak-Dev/ai-multi-instance(?:\.git)?$") {
        throw "The existing clone does not use the expected upstream repository."
    }
    $Dirty = (& $Git.Source -C $Manager status --porcelain | Out-String).Trim()
    if (-not [string]::IsNullOrWhiteSpace($Dirty)) {
        throw "The multi-account manager has local changes and will not be updated automatically."
    }
}

& $Git.Source -C $Manager fetch --depth=1 origin $PinnedCommit
if ($LASTEXITCODE -ne 0) {
    throw "Could not download the audited upstream commit."
}
& $Git.Source -C $Manager checkout --detach $PinnedCommit
if ($LASTEXITCODE -ne 0) {
    throw "Could not select the audited upstream commit."
}
$EngineHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Manager "engine.py")).Hash
$MainHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Manager "main.py")).Hash
if ($EngineHash -ne $ExpectedEngineHash -or $MainHash -ne $ExpectedMainHash) {
    throw "The downloaded files do not match the audited source hashes."
}
New-Item -ItemType Directory -Path $Profiles -Force | Out-Null

$Configured = [Environment]::GetEnvironmentVariable("CLAUDE_MULTI_INSTANCE_ROOTS", "User")
$Roots = @($Configured -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($Profiles -notin $Roots) {
    $Roots += $Profiles
}
$RootValue = $Roots -join ";"
[Environment]::SetEnvironmentVariable("CLAUDE_MULTI_INSTANCE_ROOTS", $RootValue, "User")
$env:CLAUDE_MULTI_INSTANCE_ROOTS = $RootValue
$AutoLaunch = Join-Path $StateDir "claude-multi-instance-auto-launch.json"
@{
    enabled = $true
    manager = $Manager
    commit = $PinnedCommit
    engine_sha256 = $EngineHash
    main_sha256 = $MainHash
} | ConvertTo-Json | Set-Content -LiteralPath $AutoLaunch -Encoding UTF8

$Desktop = [Environment]::GetFolderPath("DesktopDirectory")
$ShortcutPath = Join-Path $Desktop "Claude Multi-Account.lnk"
$PowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$OpenScript = Join-Path $Project "open-claude-multi-account.ps1"
$Icon = Join-Path $Project "assets\claude-codex-queue.ico"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShell
$Shortcut.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$OpenScript`""
$Shortcut.WorkingDirectory = $Project
$Shortcut.IconLocation = $Icon
$Shortcut.Description = "Manage isolated Claude profiles side by side"
$Shortcut.WindowStyle = 1
$Shortcut.Save()

Write-Output "Manager installed: $Manager"
Write-Output "Desktop shortcut created: $ShortcutPath"
Write-Output "Audited commit: $PinnedCommit"
Write-Output "Automatic launch for authenticated profiles: enabled"

if (-not $NoLaunch) {
    Start-Process -FilePath $PowerShell -ArgumentList "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$OpenScript`"" -WorkingDirectory $Project -WindowStyle Normal
}
