@echo off
cd /d "%~dp0"
if "%~1"=="" (
    where pythonw >nul 2>&1
    if %errorlevel%==0 (
        start "" /B pythonw -m src.aura.agents.aura_v4 --web --port 8000
        exit /b 0
    )
)
python -m src.aura.agents.aura_v4 --web --port 8000 %*
if errorlevel 1 pause
