$ErrorActionPreference = "Stop"

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

$Manager = Join-Path $StateDir "companions\ai-multi-instance"
$Main = Join-Path $Manager "main.py"
$Engine = Join-Path $Manager "engine.py"
$ExpectedEngineHash = "A041E3ED220D9B58C290F957C7500EE9D6FABA625B992E2878EFD9E8EAB9A3ED"
$ExpectedMainHash = "A0C29F4E5DD2685E295FD268D1BB3929DB33F28E906E8918263E079FE6410261"
if (-not (Test-Path -LiteralPath $Main)) {
    throw "Claude multi-account manager is not installed. Run install-claude-multi-account.ps1 first."
}
$EngineHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Engine).Hash
$MainHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Main).Hash
if ($EngineHash -ne $ExpectedEngineHash -or $MainHash -ne $ExpectedMainHash) {
    throw "The Claude multi-account manager has changed. Run the installer again before launching it."
}

$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -ne $PyLauncher) {
    Push-Location $Manager
    try {
        & $PyLauncher.Source -3 $Main
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

$Python = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -eq $Python) {
    throw "Python 3 for Windows was not found."
}
Push-Location $Manager
try {
    & $Python.Source $Main
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
