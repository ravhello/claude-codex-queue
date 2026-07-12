$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Startup = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $Startup "Claude + Codex Queue.lnk"
$Launcher = Join-Path $Project "start-claude-codex-queue.ps1"
$Icon = Join-Path $Project "assets\claude-codex-queue.ico"
$PowerShell = Join-Path $PSHOME "powershell.exe"

if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "Launcher non trovato: $Launcher"
}
if (-not (Test-Path -LiteralPath $Startup)) {
    New-Item -ItemType Directory -Path $Startup -Force | Out-Null
}

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShell
$Shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Launcher`""
$Shortcut.WorkingDirectory = $Project
$Shortcut.IconLocation = $Icon
$Shortcut.Description = "Avvia automaticamente Claude + Codex Queue e apre il browser"
$Shortcut.WindowStyle = 7
$Shortcut.Save()

Write-Output $ShortcutPath
