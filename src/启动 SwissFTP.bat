@echo off
rem Prefer the standalone EXE; fall back to running from source.
cd /d "%~dp0.."
if exist "dist\SwissFTP.exe" (
    start "" "dist\SwissFTP.exe" %*
    exit /b 0
)
cd /d "%~dp0"
py -3 swiss_ftp.py %*
if errorlevel 1 pause
