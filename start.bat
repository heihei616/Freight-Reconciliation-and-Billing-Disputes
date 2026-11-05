@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem -----------------------------------------------------------
rem Logistics Agent launcher
rem 1. add src to module path (no pip install required)
rem 2. open the login page in the default browser after 6s
rem 3. run the app in this window; Ctrl+C to stop
rem -----------------------------------------------------------

set "PYTHONPATH=%~dp0src"

if not exist "%~dp0.env" (
  echo [WARN] .env not found, the app will run in mock mode.
  echo        Copy .env.example to .env and fill in LLM_API_KEY for real answers.
  echo.
)

start "open-browser" /min cmd /c "timeout /t 6 /nobreak >nul & explorer http://127.0.0.1:8000"

echo ============================================================
echo   Logistics Agent
echo   Page    : http://127.0.0.1:8000
echo   Admin   : http://127.0.0.1:8000/admin
echo   Account : employee / emp123   admin / admin123
echo   Stop    : Ctrl+C
echo ============================================================
echo.

python -m logistics_agent.main

echo.
echo Service stopped.
pause
