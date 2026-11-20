@echo off
rem ============================================================
rem  Deploy after `npm run dist`: copy fresh exe + app.asar
rem  into this folder so the app runs from the repo root.
rem  Close the running console first.
rem ============================================================
setlocal
cd /d "%~dp0"

set "SRC=electron\dist-out\win-unpacked"
if not exist "%SRC%\%APPNAME%" (
    echo [ERROR] Build output not found: %SRC%
    echo Run: cd electron ^&^& npm install ^&^& npm run dist
    pause
    exit /b 2
)

for %%F in ("%SRC%\*.exe") do set "APPNAME=%%~nxF"
copy /y "%SRC%\%APPNAME%" "%APPNAME%" >nul
if errorlevel 1 (
    echo [ERROR] Copy failed. Close the running console first.
    pause
    exit /b 3
)
copy /y "%SRC%\resources\app.asar" "resources\app.asar" >nul
if not exist "resources" mkdir "resources"

echo.
echo Deployed: %APPNAME% + resources\app.asar
echo First launch may ask for admin (UAC) once.
echo.
pause
