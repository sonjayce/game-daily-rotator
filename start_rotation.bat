@echo off
rem ============================================================
rem  Rotation Launcher CLI (pure ASCII to avoid codepage issues)
rem  Double-click = full rotation. Args: check / setup / detect-packages
rem ============================================================
setlocal
cd /d "%~dp0"
set "PY="

python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%~dp0..\AzurLaneAutoScript\toolkit\python.exe" set "PY=%~dp0..\AzurLaneAutoScript\toolkit\python.exe"
if not defined PY if exist "%~dp0..\MaaAutoNaruto-win-x86_64-v1.4.8\python\python.exe" set "PY=%~dp0..\MaaAutoNaruto-win-x86_64-v1.4.8\python\python.exe"

if not defined PY (
    echo [ERROR] No Python 3.7+ interpreter found.
    pause
    exit /b 2
)

echo Using: %PY%
set "PYTHONIOENCODING=utf-8"

if "%~1"=="" (
    %PY% "%~dp0rotate.py" run
) else (
    %PY% "%~dp0rotate.py" %*
)

echo.
echo ============ Exit code %ERRORLEVEL% ============
pause
