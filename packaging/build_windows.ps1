# Windows 构建脚本
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\agent-team.spec

Write-Host ""
Write-Host "构建完成："
Write-Host "  dist\MultiAgentCodegenTeam\MultiAgentCodegenTeam.exe"
Write-Host ""
Write-Host "如需安装包，请用 Inno Setup 编译 packaging\installer\windows.iss"
