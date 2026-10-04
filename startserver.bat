@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setupapp.bat first.
  exit /b 1
)
.venv\Scripts\python.exe -m billing --run-server
exit /b %errorlevel%
