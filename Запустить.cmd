@echo off
chcp 65001 >nul
set "INTERVIEW_APP=%~dp0server.py"
py -3 --version >nul 2>&1
if not errorlevel 1 (
  py -3 "%INTERVIEW_APP%"
) else (
  python "%INTERVIEW_APP%"
)
pause