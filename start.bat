@echo off
rem Double-click to start Healer Review and open it in your browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up...
    python -m venv .venv
    ".venv\Scripts\python" -m pip install -q -r requirements.txt
)
start "" http://127.0.0.1:8000
".venv\Scripts\python" -m uvicorn app.web:app --host 127.0.0.1 --port 8000
