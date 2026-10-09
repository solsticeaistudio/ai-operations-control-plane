if (-not (Test-Path .venv)) { py -m venv .venv }
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Start-Process powershell -ArgumentList '-NoExit','-Command','uvicorn app.main:app --host 127.0.0.1 --port 8080'
Start-Sleep -Seconds 3
Start-Process 'http://127.0.0.1:8080'
