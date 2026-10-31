# scripts/setup.ps1 - Jobhunt one-command setup for Windows
$ErrorActionPreference = "Stop"

Write-Host "=== Jobhunt Setup (Windows) ===" -ForegroundColor Cyan

# 1. Check Python version (>= 3.12)
$pyCommand = $null
$candidates = @("python", "python3", "py -3.14", "py -3.13", "py -3.12", "py -3", "py")
foreach ($cmd in $candidates) {
    try {
        $checkCmd = "$cmd -c `"import sys; exit(0 if sys.version_info >= (3, 12) else 1)`""
        cmd /c $checkCmd 2>$null
        if ($LASTEXITCODE -eq 0) {
            $pyCommand = $cmd
            break
        }
    } catch {}
}

if (-not $pyCommand) {
    Write-Error "No suitable Python runtime found (>= 3.12 required). Please install Python 3.12 or newer from https://www.python.org/downloads/"
    exit 1
}

# 2. Create .venv if not exists
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment .venv..." -ForegroundColor Yellow
    Invoke-Expression "$pyCommand -m venv .venv"
}
$venvPython = ".venv\Scripts\python.exe"

# 3. Install pinned Python dependencies
Write-Host "Installing Python dependencies from requirements.lock..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip --quiet
if (Test-Path "requirements.lock") {
    & $venvPython -m pip install -r requirements.lock -e ".[api,gmail,matching,dev]" --quiet
} else {
    & $venvPython -m pip install -e ".[api,gmail,matching,dev]" --quiet
}

# 4. Check Node >= 22.12
if (Get-Command node -ErrorAction SilentlyContinue) {
    $nodeVer = (node -v).Trim().TrimStart('v')
    $nodeParts = $nodeVer.Split('.')
    $nodeOk = ([int]$nodeParts[0] -gt 22) -or (([int]$nodeParts[0] -eq 22) -and ([int]$nodeParts[1] -ge 12))
    if (-not $nodeOk) {
        Write-Warning "Node.js $nodeVer detected. Jobhunt requires Node.js >= 22.12 (22 LTS) to build and test the frontend. Please upgrade from https://nodejs.org/"
    } else {
        Write-Host "Node.js v$nodeVer detected (>= 22.12 OK)" -ForegroundColor Green
    }
} else {
    Write-Warning "Node.js not found. To build web UI assets, please install Node.js >= 22.12 (22 LTS) from https://nodejs.org/"
}

# 5. Build web UI if node and npm are available
if ((Get-Command npm -ErrorAction SilentlyContinue) -and (Test-Path "web")) {
    Push-Location "web"
    try {
        if (-not (Test-Path "node_modules")) {
            Write-Host "Installing frontend dependencies (npm ci)..." -ForegroundColor Yellow
            npm ci --quiet
        }
        Write-Host "Building frontend assets (npm run build)..." -ForegroundColor Yellow
        npm run build --silent
    } finally {
        Pop-Location
    }
}

# 6. Ensure Tectonic is present
Write-Host "Checking Tectonic LaTeX engine..." -ForegroundColor Yellow
& $venvPython -m jobhunt.platform.tectonic_install

# 7. Create data/ and .secrets/
New-Item -ItemType Directory -Force -Path "data" | Out-Null
New-Item -ItemType Directory -Force -Path ".secrets" | Out-Null

# 8. Run jobhunt doctor
Write-Host "`nRunning system doctor..." -ForegroundColor Cyan
& $venvPython -m jobhunt doctor

# 9. Completion hint. Setup never opens a browser or starts a server; start.ps1 does that on a free port.
Write-Host "`n=== Setup Complete! ===" -ForegroundColor Green
Write-Host "Run: .\scripts\start.ps1   (picks a free port; add -NoBrowser to skip opening the browser)" -ForegroundColor Cyan
Write-Host "Or:  .\.venv\Scripts\python.exe -m jobhunt serve" -ForegroundColor Cyan
