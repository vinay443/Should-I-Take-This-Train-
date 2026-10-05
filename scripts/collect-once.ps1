# Runs the live collector once and appends to data\sitt.duckdb. See docs/collector.md.
# Windows Task Scheduler calls this every 15 minutes; you can also run it by hand.
$ErrorActionPreference = "Stop"

# The repository is the folder above this script, wherever it was cloned.
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not on PATH for this user. Install it from https://docs.astral.sh/uv/ or add it to PATH."
    exit 2
}

uv run sitt-collect
exit $LASTEXITCODE
