$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Startup = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $Startup "Claude + Codex Queue.lnk"
$Launcher = Join-Path $Project "start-claude-codex-queue-hidden.vbs"
$Icon = Join-Path $Project "assets\claude-codex-queue.ico"
$WScript = Join-Path $env:WINDIR "System32\wscript.exe"

if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "Launcher non trovato: $Launcher"
}
if (-not (Test-Path -LiteralPath $Startup)) {
    New-Item -ItemType Directory -Path $Startup -Force | Out-Null
}

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $WScript
$Shortcut.Arguments = "//B //Nologo `"$Launcher`""
$Shortcut.WorkingDirectory = $Project
$Shortcut.IconLocation = $Icon
$Shortcut.Description = "Avvia Claude + Codex Queue interamente in background e apre il browser"
$Shortcut.WindowStyle = 7
$Shortcut.Save()

Write-Output $ShortcutPath
