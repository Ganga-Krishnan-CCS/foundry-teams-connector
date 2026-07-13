# Runs a python entrypoint with the pip-installed Azure CLI on PATH, so
# DefaultAzureCredential's AzureCliCredential fallback can find `az`.
#   .\run_local.ps1 test_foundry_pipeline.py
#   .\run_local.ps1 app.py
param([Parameter(Mandatory = $true)][string]$Script, [Parameter(ValueFromRemainingArguments = $true)]$Rest)

$root = $PSScriptRoot
$env:PATH = "$root\.venv-azcli\Scripts;$env:PATH"
$env:PYTHONUTF8 = "1"
& "$root\.venv\Scripts\python.exe" "$root\$Script" @Rest
