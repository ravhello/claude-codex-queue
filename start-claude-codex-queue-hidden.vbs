Option Explicit

Dim shell, fso, project, launcher, powershell, quote, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

project = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(project, "start-claude-codex-queue.ps1")
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
quote = Chr(34)
command = quote & powershell & quote & _
    " -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & _
    quote & launcher & quote

shell.Run command, 0, False
