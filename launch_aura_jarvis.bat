@echo off
cd /d "%~dp0"
set AURA_ENABLE_DESKTOP_MCP=1
set AURA_ENABLE_JARVIS_MCP=1
set AURA_ENABLE_BROWSER_MCP=1
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" /B pythonw -m src.aura.agents.aura_v4 --web --overlay --mode rw --port 8000
    exit /b 0
)
python -m src.aura.agents.aura_v4 --web --overlay --mode rw --port 8000
