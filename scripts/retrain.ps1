# Runs sitt-retrain for Windows Task Scheduler and appends its output to
# data\logs\retrain.log. See docs/retraining.md.
#
# Most weeks this only reports that there isn't enough real data yet. When there is, it
# trains a model and promotes it only if it beats the baselines.
$ErrorActionPreference = "Stop"

# The repository is the folder above this script, wherever it was cloned.
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not on PATH for this user. Install it from https://docs.astral.sh/uv/ or add it to PATH."
    exit 2
}

$log = "data\logs\retrain.log"
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

"--- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') sitt-retrain" | Out-File $log -Append -Encoding utf8
$ErrorActionPreference = "Continue"
uv run sitt-retrain 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
exit $LASTEXITCODE
