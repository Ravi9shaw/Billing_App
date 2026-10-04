@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  set "BILLING_PYTHON=python"
) else (
  set "BILLING_PYTHON=py -3.11"
)
%BILLING_PYTHON% -c "import sys; assert sys.version_info[:2] == (3,11), 'Install Python 3.11 for this build'"
if errorlevel 1 goto :fail
if not exist ".venv\Scripts\python.exe" (
  %BILLING_PYTHON% -m venv .venv
  if errorlevel 1 goto :fail
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :fail
.venv\Scripts\python.exe -m billing --setup
if errorlevel 1 goto :fail
echo Setup complete. Run startapp.bat or build_windows.bat.
exit /b 0
:fail
echo Setup failed. Fix the error above and rerun; existing shop data is preserved.
exit /b 1
