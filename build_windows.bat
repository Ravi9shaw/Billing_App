@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setupapp.bat first.
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --onefile --windowed --name ClothShopBilling --icon app\assets\icon.ico --add-data "billing\web;billing\web" --add-data "app\assets;app\assets" --collect-all uvicorn --collect-all fastapi --collect-all starlette --collect-all pydantic --collect-submodules reportlab main.py
if errorlevel 1 exit /b 1
echo Built dist\ClothShopBilling.exe. Data and configuration stay in the user profile.
exit /b 0
