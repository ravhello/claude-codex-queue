$launcher = Join-Path $PSScriptRoot "start-claude-codex-queue-hidden.vbs"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
Start-Process -FilePath $wscript -ArgumentList @("//B", "//Nologo", "`"$launcher`"") -WindowStyle Hidden
