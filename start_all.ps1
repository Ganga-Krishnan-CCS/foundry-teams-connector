# Starts the relay + dev tunnel in their own windows (independent of any IDE/session).
#   .\start_all.ps1
# Requires: .env filled in, devtunnel already logged in (tools\devtunnel.exe user login).
# The tunnel below is the PERSISTENT named tunnel whose URL is registered on the
# Azure Bot (ccs-foundry-relay-6109). If you ever host a different tunnel id, update
# the bot: az bot update -n ccs-foundry-relay-6109 -g rg-johnbaby-6109_ai -e <new-url>/api/messages

$root = $PSScriptRoot

# 1. Relay (own window so you can watch the logs)
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$env:PATH='$root\.venv-azcli\Scripts;' + `$env:PATH; `$env:PYTHONUTF8='1'; Set-Location '$root'; .\.venv\Scripts\python.exe app.py"
) -WindowStyle Normal

# 2. Dev tunnel (own window; reuses the tunnel already registered with the bot)
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$root'; .\tools\devtunnel.exe host sneaky-plane-98rxds0"
) -WindowStyle Normal

Start-Sleep -Seconds 8
try {
    $h = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:3978/healthz" -TimeoutSec 10
    Write-Host "Relay: $($h.Content)"
} catch { Write-Host "Relay not responding yet - check its window." }
try {
    $t = Invoke-WebRequest -UseBasicParsing -Uri "https://1n3zpnkz-3978.inc1.devtunnels.ms/healthz" -TimeoutSec 20
    Write-Host "Tunnel: $($t.Content)"
    Write-Host "All up. Teams messaging endpoint: https://1n3zpnkz-3978.inc1.devtunnels.ms/api/messages"
} catch { Write-Host "Tunnel not responding yet - check its window." }
