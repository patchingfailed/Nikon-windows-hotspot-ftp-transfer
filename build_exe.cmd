@echo off
rem Builds NikonFTPTransfer.exe locally (dist\NikonFTPTransfer.exe).
cd /d "%~dp0"
python -m pip install --upgrade -r requirements.txt pyinstaller || goto :fail
python tests\test_progress.py || goto :fail
python tests\test_i18n.py || goto :fail
python tests\test_release.py || goto :fail
python tests\test_network.py || goto :fail
python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name NikonFTPTransfer ^
  --icon assets\icon.ico ^
  --add-data "ui.html;." ^
  --add-data "locales;locales" ^
  nikon_ftp_transfer.py || goto :fail
echo.
echo Done: dist\NikonFTPTransfer.exe
pause
exit /b 0
:fail
echo.
echo Build failed.
pause
exit /b 1
