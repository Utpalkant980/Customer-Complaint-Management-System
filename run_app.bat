@echo off
setlocal
cd /d "%~dp0"

curl.exe --silent --fail "http://127.0.0.1:8000/api/health" >nul 2>&1
if not errorlevel 1 goto open_app

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python and make sure it is available on PATH.
    pause
    exit /b 1
)

start "Complaint Management Backend" /D "%~dp0" python app.py

for /L %%i in (1,1,20) do (
    curl.exe --silent --fail "http://127.0.0.1:8000/api/health" >nul 2>&1
    if not errorlevel 1 goto open_app
    timeout /t 1 /nobreak >nul
)

echo The Python backend did not start. Check the Complaint Management Backend window.
pause
exit /b 1

:open_app
start "" "http://127.0.0.1:8000"
endlocal
