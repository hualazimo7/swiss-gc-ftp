@echo off
rem FileZilla / Windows Explorer: ftp://127.0.0.1:2121  (any user/pass)
cd /d "%~dp0.."
if exist "dist\SwissFTP.exe" (
    "%~dp0..\dist\SwissFTP.exe" --cli --gateway %*
    goto :eof
)
cd /d "%~dp0"
py -3 swiss_ftp.py --cli --gateway %*
pause
