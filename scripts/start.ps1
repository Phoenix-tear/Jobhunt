# scripts/start.ps1 - Start Jobhunt local server on Windows
# Picks a free port (8000, then 8001-8010) and never touches an app that is already running.
# Usage: .\scripts\start.ps1 [-NoBrowser]
param([switch]$NoBrowser)
$ErrorActionPreference = "Stop"

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = "python"
}

$launchArgs = @()
if ($NoBrowser) { $launchArgs += "--no-browser" }
& $py -m jobhunt.platform.launch @launchArgs @args
exit $LASTEXITCODE
