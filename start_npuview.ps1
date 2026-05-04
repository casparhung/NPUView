$ErrorActionPreference = 'Stop'

# Always run from the script directory
Set-Location $PSScriptRoot

$python = 'python'
if (Test-Path '.venv\Scripts\python.exe') {
    $python = '.venv\Scripts\python.exe'
}

& $python '-c' 'import psutil' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[NPUView] Missing Python packages. Installing from requirements.txt ...'
    & $python '-m' 'pip' 'install' '--no-cache-dir' '-r' 'requirements.txt'
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[NPUView] Failed to install requirements.' -ForegroundColor Red
        Read-Host 'Press Enter to close'
        exit 1
    }
}

Write-Host "[NPUView] Using Python: $python"
Write-Host "[NPUView] Starting server on http://localhost:2700"
& $python 'app.py'

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host '[NPUView] Server exited with an error.' -ForegroundColor Red
    Read-Host 'Press Enter to close'
}
