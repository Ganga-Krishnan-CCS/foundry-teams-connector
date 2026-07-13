# One-shot setup once IT delivers the new app registration.
# Usage:
#   .\create_bot.ps1 -AppId <client-id-from-IT> -TunnelHost <xyz.devtunnels.ms>
# Then upload teams-app\foundry-relay.zip to Teams (or send it to the Teams admin).
param(
    [Parameter(Mandatory = $true)][string]$AppId,
    [Parameter(Mandatory = $true)][string]$TunnelHost,
    [string]$BotName = "foundry-relay-bot",
    [string]$ResourceGroup = "rg-johnbaby-6109_ai",
    [string]$TenantId = "4a531b0c-72f9-4a28-b4b8-9f40efbcd39e"
)

$az = Join-Path $PSScriptRoot ".venv-azcli\Scripts\az.bat"
$endpoint = "https://$TunnelHost/api/messages"

Write-Host "Creating Azure Bot '$BotName' (SingleTenant, app $AppId) -> $endpoint"
& $az bot create --name $BotName --resource-group $ResourceGroup `
    --app-type SingleTenant --appid $AppId --tenant-id $TenantId `
    --endpoint $endpoint --sku F0
if (-not $?) { throw "bot create failed" }

Write-Host "Enabling Teams channel"
& $az bot msteams create --name $BotName --resource-group $ResourceGroup
if (-not $?) { throw "teams channel failed" }

Write-Host "Building Teams app package"
$pkgDir = Join-Path $PSScriptRoot "teams-app"
$staging = Join-Path $env:TEMP "teams-app-staging"
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $staging | Out-Null
(Get-Content (Join-Path $pkgDir "manifest.json") -Raw) -replace "__BOT_APP_ID__", $AppId |
    Set-Content -Encoding utf8 (Join-Path $staging "manifest.json")
Copy-Item (Join-Path $pkgDir "color.png"), (Join-Path $pkgDir "outline.png") $staging
$zip = Join-Path $pkgDir "foundry-relay.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path "$staging\*" -DestinationPath $zip
Write-Host "Done. Teams app package: $zip"
Write-Host "Next: fill CLIENTSECRET in .env, start the relay + tunnel, upload the zip in Teams."
