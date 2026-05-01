@echo off
setlocal

REM Always run from the script directory
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
)

"%PYTHON%" -c "import psutil" >nul 2>&1
if errorlevel 1 (
    echo [NPUView] Missing Python packages. Installing from requirements.txt ...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [NPUView] Failed to install requirements.
        pause
        exit /b 1
    )
)

echo [NPUView] Using Python: %PYTHON%
echo [NPUView] Starting server on http://localhost:2700
"%PYTHON%" app.py

if errorlevel 1 (
    echo.
    echo [NPUView] Server exited with an error.
    pause
)

endlocal
