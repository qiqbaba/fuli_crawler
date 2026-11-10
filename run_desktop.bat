@echo off
chcp 65001 >nul
title Fuli Workbench (FastAPI + React)
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if exist "%~dp0desktop\dist\index.html" goto prod
goto dev

:prod
echo ========================================================
echo   Production mode: http://127.0.0.1:8000
echo ========================================================
start "Fuli API" /min "%PY%" -m uvicorn bridge.api:app --host 127.0.0.1 --port 8000
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8000
echo.
echo API server runs in the minimized window "Fuli API".
echo Close that window to stop the server.
echo.
pause
goto end

:dev
echo ========================================================
echo   Dev mode: http://127.0.0.1:4173
echo   Frontend build not found, starting Vite dev server
echo ========================================================
start "Fuli API" /min "%PY%" -m uvicorn bridge.api:app --host 127.0.0.1 --port 8000
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:4173
cd /d "%~dp0desktop"
call npm run dev
goto end

:end
