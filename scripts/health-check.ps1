# Runs sitt-health for Windows Task Scheduler and appends its output to
# data\logs\health.log. See docs/collector.md.
#
#   scripts\health-check.ps1 -Mode summary   the daily Telegram summary
#   scripts\health-check.ps1 -Mode alert     a Telegram message only if something is wrong
#
# Nothing is really sent unless .env has SITT_TELEGRAM_SEND=true.
param(
    [ValidateSet("summary", "alert")]
    [string]$Mode = "alert"
)
$ErrorActionPreference = "Stop"

# The repository is the folder above this script, wherever it was cloned.
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not on PATH for this user. Install it from https://docs.astral.sh/uv/ or add it to PATH."
    exit 2
}

$log = "data\logs\health.log"
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null
$flag = if ($Mode -eq "summary") { "--telegram" } else { "--alert-only" }

"--- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') sitt-health $flag" | Out-File $log -Append -Encoding utf8
$ErrorActionPreference = "Continue"
uv run sitt-health $flag 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
exit $LASTEXITCODE
