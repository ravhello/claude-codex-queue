$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("DesktopDirectory")
$ShortcutPath = Join-Path $Desktop "Claude + Codex Queue.lnk"
$LegacyShortcutPath = Join-Path $Desktop "Claude VS Code Queue.lnk"
$Launcher = Join-Path $Project "start-claude-codex-queue.cmd"
$Icon = Join-Path $Project "assets\claude-codex-queue.ico"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Launcher
$Shortcut.Arguments = ""
$Shortcut.WorkingDirectory = $Project
$Shortcut.IconLocation = $Icon
$Shortcut.Description = "Avvia Claude + Codex Queue e apre il browser"
$Shortcut.WindowStyle = 7
$Shortcut.Save()

if (Test-Path -LiteralPath $LegacyShortcutPath) {
    $LegacyShortcut = $Shell.CreateShortcut($LegacyShortcutPath)
    $LegacyTargets = @(
        (Join-Path $Project "start-claude-queue.cmd"),
        (Join-Path $Project "start-claude-codex-queue.cmd")
    )
    if ($LegacyShortcut.TargetPath -in $LegacyTargets) {
        Remove-Item -LiteralPath $LegacyShortcutPath -Force
    }
}

Write-Output $ShortcutPath

$AutoStartInstaller = Join-Path $Project "install-autostart.ps1"
& $AutoStartInstaller
