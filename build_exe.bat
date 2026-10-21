@echo off
rem Builds dist\YeelightAmbilight.exe and dist\uninstall.exe (no console windows). Run from the project folder.
rem icon.ico is the app icon (made by icon_art.py; run "python icon_art.py" to recreate it).
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name YeelightAmbilight ^
  --icon icon.ico ^
  --hidden-import pystray._win32 ^
  tray_app.py || exit /b 1
python -m PyInstaller --noconfirm --onefile --noconsole --name uninstall --icon icon.ico uninstall.py || exit /b 1
echo.
echo Done: dist\YeelightAmbilight.exe and dist\uninstall.exe
