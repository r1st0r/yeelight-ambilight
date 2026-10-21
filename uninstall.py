#!/usr/bin/env python3
"""
Yeelight Ambilight - uninstaller (Windows).

Removes what the application put on the computer outside its own folder:
  * the "Start with Windows" entry (HKCU\\...\\Run\\YeelightAmbilight)
  * the settings and the log (%LOCALAPPDATA%\\YeelightAmbilight), after asking
  * the remembered lamp address (~\\.yeelight_ambilight_lamp.json)
It cannot delete itself or the program folder: delete that folder by hand afterwards.

Build: build_exe.bat creates dist\\uninstall.exe. Without a build: python uninstall.py
"""

import ctypes
import os
import shutil
import subprocess
import sys

APP_NAME = "YeelightAmbilight"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
LAMP_CACHE = os.path.join(os.path.expanduser("~"), ".yeelight_ambilight_lamp.json")
MB_YESNO, MB_ICONQUESTION, MB_ICONINFO, MB_ICONERROR = 0x04, 0x20, 0x40, 0x10
IDYES = 6


def box(text, flags=MB_ICONINFO):
    if os.name == "nt":
        return ctypes.windll.user32.MessageBoxW(0, text, "Yeelight Ambilight uninstaller", flags)
    print(text)
    return IDYES


def is_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {APP_NAME}.exe", "/NH"], capture_output=True,
                         text=True, creationflags=0x08000000).stdout
    return f"{APP_NAME}.exe".lower() in out.lower()


def remove_autostart() -> str:
    try:
        import winreg
    except ImportError:
        return "autostart: nothing to do on this system"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        return "autostart entry removed"
    except FileNotFoundError:
        return "autostart entry was not set"
    except OSError as exc:
        return f"autostart entry NOT removed: {exc}"


def main() -> None:
    if is_running():
        box("Yeelight Ambilight is still running.\n\nRight-click its icon in the notification area, choose "
            "Quit (this also switches the lamp off), and run the uninstaller again.", MB_ICONERROR)
        sys.exit(1)
    if box("Remove Yeelight Ambilight from Windows startup and delete its remembered lamp address?",
           MB_YESNO | MB_ICONQUESTION) != IDYES:
        return
    report = [remove_autostart()]
    try:
        os.remove(LAMP_CACHE)
        report.append("remembered lamp address removed")
    except FileNotFoundError:
        pass
    except OSError as exc:
        report.append(f"lamp address NOT removed: {exc}")
    if os.path.isdir(DATA_DIR) and box(f"Also delete the settings and the log?\n\n{DATA_DIR}",
                                       MB_YESNO | MB_ICONQUESTION) == IDYES:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        report.append("settings and log deleted" if not os.path.isdir(DATA_DIR) else "settings and log: some files stay")
    report.append("\nThe program itself is not deleted: remove its folder (YeelightAmbilight.exe, uninstall.exe) by hand.")
    box("\n".join(report))


if __name__ == "__main__":
    main()
