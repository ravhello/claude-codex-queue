$ErrorActionPreference = "Stop"

$Url = "http://127.0.0.1:8765/"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path

# WSL interop strips backslashes from arguments, so pass the project path with
# forward slashes; wslpath accepts them and returns the mounted /mnt/... path.
$ProjectFwd = $Project -replace '\\', '/'
$WslDir = (& wsl.exe wslpath -a -u $ProjectFwd | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($WslDir)) {
    throw "Conversione del percorso WSL fallita per '$Project'"
}
$WslScript = "$WslDir/start-visual.sh"

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
