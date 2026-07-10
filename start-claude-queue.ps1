$ErrorActionPreference = "Stop"

$Url = "http://127.0.0.1:8765/"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$WslScript = (wsl.exe wslpath -a -u $Project).Trim() + "/start-visual.sh"

$result = & wsl.exe bash $WslScript
if ($LASTEXITCODE -ne 0) {
    throw "Avvio WSL fallito: $result"
}

for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/queue" -TimeoutSec 5 | Out-Null
        Start-Process $Url
        exit 0
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

throw "Server non raggiungibile su $Url"
