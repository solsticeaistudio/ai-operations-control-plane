@echo off
setlocal
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -r requirements.txt
start "Solstice AI Operations" cmd /k "uvicorn app.main:app --host 127.0.0.1 --port 8080"
timeout /t 3 >nul
start http://127.0.0.1:8080
