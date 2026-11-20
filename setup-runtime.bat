@echo off
rem ============================================================
rem  One-time: download the embedded Python 3.11 runtime that
rem  the packaged console expects at runtime\python\python.exe
rem  (uses Windows 10+ built-in curl and tar)
rem ============================================================
setlocal
cd /d "%~dp0"

if exist "runtime\python\python.exe" (
    echo Runtime already present: runtime\python\python.exe
    pause
    exit /b 0
)

set "PYVER=3.11.9"
set "URL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip"

echo Downloading %URL% ...
if not exist "runtime\python" mkdir "runtime\python"
curl -L -o "%TEMP%\py-embed.zip" "%URL%" || (
    echo [ERROR] Download failed. Download manually and unzip into runtime\python\
    pause
    exit /b 2
)
tar -xf "%TEMP%\py-embed.zip" -C "runtime\python" || (
    echo [ERROR] Unzip failed. Unzip manually into runtime\python\
    pause
    exit /b 3
)
del "%TEMP%\py-embed.zip"

if exist "runtime\python\python.exe" (
    echo OK: runtime\python\python.exe ready.
) else (
    echo [ERROR] python.exe not found after unzip.
)
pause
