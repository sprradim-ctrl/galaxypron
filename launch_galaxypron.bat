@echo off
REM galaxypron - Edge AI Agent
REM Starts the 24/7 learning server (auto-trains from Wikipedia) and opens
REM the app in your default browser. The browser version is fully reliable;
REM the buttons + progress bars work there.
setlocal
set "PROJECT=C:\Users\User\Documents\Default Project"
set "PY=%PROJECT%\venv\Scripts\python.exe"
set "URL=http://127.0.0.1:5000/"

if not exist "%PY%" set "PY=C:\Users\User\Python312\python.exe"

REM Start the server (24/7 trainer auto-starts) if it isn't already running
>nul 2>&1 curl -s --max-time 2 "%URL%api/status" || (
    echo Starting galaxypron server...
    start "galaxypron server" /min cmd /c ""%PY%" "%PROJECT%\server\app.py""
    rem wait for the server to come up
    set "tries=0"
    :wait
    >nul 2>&1 curl -s --max-time 2 "http://127.0.0.1:5000/api/status"
    if not errorlevel 1 goto ready
    set /a tries+=1
    if %tries% GEQ 30 goto ready
    timeout /t 1 /nobreak >nul
    goto wait
    :ready
)

REM Open the app in the default browser
start "" "%URL%"
endlocal