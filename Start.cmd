@echo off
rem Starts Nikon Windows Hotspot FTP Transfer from source (needs Python 3.10+).
rem No Python? Use NikonFTPTransfer.exe from the GitHub releases.
title Nikon FTP Transfer
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python was not found. Either install Python 3
  echo or use the ready-made NikonFTPTransfer.exe from the releases.
  pause
  exit /b 1
)
python -c "import pyftpdlib" 2>nul || (
  echo pyftpdlib is missing - installing ...
  python -m pip install --user -r requirements.txt
)
python nikon_ftp_transfer.py
echo.
pause
