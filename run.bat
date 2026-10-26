@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    py -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
    echo [setup] Downloading Chromium for Playwright...
    ".venv\Scripts\python.exe" -m playwright install chromium || goto :error
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [setup] Created .env - set DATABASE_URL before starting.
)

echo.
echo   App:      http://127.0.0.1:8000
echo   API docs: http://127.0.0.1:8000/docs
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
goto :eof

:error
echo.
echo Setup failed. See the messages above.
pause
