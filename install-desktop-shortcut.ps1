$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("DesktopDirectory")
$ShortcutPath = Join-Path $Desktop "Claude VS Code Queue.lnk"
$Launcher = Join-Path $Project "start-claude-queue.cmd"
$Icon = Join-Path $Project "assets\claude-queue.ico"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Launcher
$Shortcut.Arguments = ""
$Shortcut.WorkingDirectory = $Project
$Shortcut.IconLocation = $Icon
$Shortcut.Description = "Avvia Claude VS Code Queue e apre il browser"
$Shortcut.WindowStyle = 7
$Shortcut.Save()

Write-Output $ShortcutPath
