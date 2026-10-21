@echo off
rem Builds dist\YeelightAmbilight.exe (no console window). Run from the project folder.
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name YeelightAmbilight ^
  --hidden-import pystray._win32 ^
  tray_app.py || exit /b 1
echo.
echo Done: dist\YeelightAmbilight.exe
