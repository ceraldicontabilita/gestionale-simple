@echo off
echo ============================================
echo   Ceraldi Group ERP — Avvio Sistema
echo ============================================
echo.

cd /d "%~dp0"

REM Controlla se le dipendenze sono installate
python -c "import fastapi" 2>nul
if errorlevel 1 (
    echo Installazione dipendenze Python...
    pip install -r requirements.txt
)

echo.
echo [1/1] Avvio backend FastAPI su http://localhost:8000
echo       Swagger docs: http://localhost:8000/docs
echo.
echo Premi CTRL+C per fermare
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
