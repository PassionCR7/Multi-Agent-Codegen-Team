@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\agent-team.spec
echo.
echo Build done: dist\MultiAgentCodegenTeam\MultiAgentCodegenTeam.exe
pause
