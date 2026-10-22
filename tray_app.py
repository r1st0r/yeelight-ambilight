#!/usr/bin/env python3
"""
Yeelight Ambilight - tray application (preview, Windows).

Starts the Ambilight engine in the background and puts an icon in the notification area:

    python tray_app.py            (with a console, for the first try)
    pythonw tray_app.py           (no console window)

Double-click the icon for the control window (rear and front light switches, front brightness and
color temperature). Menu: start with Windows, debug console, open the log, quit.
The lamp is switched off when Windows shuts down, restarts or logs off.
Everything the engine reports is written to %LOCALAPPDATA%\\YeelightAmbilight\\ambilight.log.
"""

import argparse
import ctypes
import os
import sys
import threading
import time

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:          # reported properly in main()
    pystray = None
    Image = ImageDraw = None

from ambilight_core import VERSION, Engine, load_config
from icon_art import lamp_layer
from panel import Panel

APP_NAME = "YeelightAmbilight"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
LOG_FILE = os.path.join(LOG_DIR, "ambilight.log")
SETTINGS_FILE = os.path.join(LOG_DIR, "ui_settings.json")

_mutex_handle = None         # keeps the single-instance mutex alive


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

class FileLog:
    """Appends timestamped lines to a log file; the file is rotated at about 1 MB."""

    def __init__(self, path: str, limit: int = 1_000_000):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        with self._lock:
            for line in str(message).rstrip("\n").split("\n"):
                self._fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")


def message_box(text: str, icon: int = 0x40) -> None:
    """A message box on Windows (there is no console under pythonw); stderr elsewhere."""
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(0, text, "Yeelight Ambilight", icon)
    else:
        print(text, file=sys.stderr)


def fatal(text: str) -> None:
    message_box(text, 0x10)
    sys.exit(1)


def already_running() -> bool:
    """True if another tray instance exists (two instances would fight over the lamp)."""
    global _mutex_handle
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\YeelightAmbilightTray")
    return kernel32.GetLastError() == 183          # ERROR_ALREADY_EXISTS


def launch_command(config_path=None) -> str:
    """The command line that starts this application at login (pythonw: no console window)."""
    if getattr(sys, "frozen", False):                 # built with PyInstaller: the exe is the program
        command = f'"{sys.executable}"'
        if config_path:
            command += f' --config "{os.path.abspath(config_path)}"'
        return command
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            exe = candidate
    command = f'"{exe}" "{os.path.abspath(__file__)}"'
    if config_path:
        command += f' --config "{os.path.abspath(config_path)}"'
    return command


def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def autostart_enabled() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool, config_path=None) -> None:
    winreg = _winreg()
    if winreg is None:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command(config_path))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def make_icon():
    """A miniature of the light bar (no status marks: the status lives in the app window)."""
    return lamp_layer(256).resize((64, 64), Image.LANCZOS)


class SessionEndWatcher:
    """Switches the lamp off when Windows shuts down, restarts or logs off.

    Windows tells every top-level window about that with WM_QUERYENDSESSION and WM_ENDSESSION;
    a program without such a window (like this one under pythonw) is simply terminated and the
    lamp keeps shining. So a hidden window listens for the messages and runs `on_ending`
    (stop the engine, which switches the rear light off) while Windows waits for it.
    """

    WM_QUERYENDSESSION = 0x0011
    WM_ENDSESSION = 0x0016

    def __init__(self, on_ending, on_cancelled, log):
        self.on_ending = on_ending
        self.on_cancelled = on_cancelled
        self.log = log
        self._worker = None

    def start(self) -> None:
        if os.name == "nt":
            threading.Thread(target=self._run, name="session-watcher", daemon=True).start()

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as exc:          # the lamp then simply stays as it is at shutdown
            self.log(f"[tray] the shutdown watcher failed: {exc!r}")

    def _loop(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        lresult = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", wndproc_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = lresult
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = lresult
        user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == self.WM_QUERYENDSESSION:
                    self.log("[tray] Windows is ending the session: switching the lamp off")
                    user32.ShutdownBlockReasonCreate(hwnd, "Switching the lamp off")
                    self._worker = threading.Thread(target=self.on_ending, daemon=True)
                    self._worker.start()
                    return 1                                  # TRUE: do not veto the shutdown
                if msg == self.WM_ENDSESSION:
                    if wparam:                                # the session really ends
                        if self._worker is not None:
                            self._worker.join(4.5)            # Windows terminates us right after this
                        self.log("[tray] session end: the lamp has been switched off")
                    else:                                     # the shutdown was cancelled
                        self.log("[tray] the shutdown was cancelled: starting again")
                        self.on_cancelled()
                    user32.ShutdownBlockReasonDestroy(hwnd)
                    return 0
            except Exception as exc:
                self.log(f"[tray] session message handler: {exc!r}")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = wndproc_type(wndproc)                          # must stay referenced
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.hInstance = hinstance
        wc.lpszClassName = "YeelightAmbilightSessionWatcher"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "Yeelight Ambilight", 0, 0, 0, 0, 0,
                                      None, None, hinstance, None)
        if not hwnd:
            raise ctypes.WinError()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

# ----------------------------------------------------------------------
# The application
# ----------------------------------------------------------------------

class TrayApp:
    def __init__(self, engine, config_path=None, panel=None):
        self.engine = engine
        self.panel = panel
        self.config_path = config_path
        self.icon = None

    # --- state shown to the user ---------------------------------------
    def status_text(self) -> str:
        status = self.engine.status
        if status == "running":
            return "Running" if self.engine.ambilight_on else "Ambilight is off"
        return {
            "connecting": "Connecting to the lamp...",
            "lamp not found": "Lamp not found, retrying",
            "error": "Error, retrying (see the log)",
            "stopped": "Stopped",
        }.get(status, status)

    def refresh(self) -> None:
        icon = self.icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:
            pass

    def on_status(self, _status: str) -> None:
        self.refresh()

    # --- menu actions ----------------------------------------------------
    def toggle_ambilight(self, icon=None, item=None) -> None:
        self.engine.set_ambilight(not self.engine.ambilight_on)
        self.refresh()

    def front_on(self, icon=None, item=None) -> None:
        self.engine.set_front_power(True)

    def front_off(self, icon=None, item=None) -> None:
        self.engine.set_front_power(False)

    def toggle_autostart(self, icon=None, item=None) -> None:
        try:
            set_autostart(not autostart_enabled(), self.config_path)
        except OSError as exc:
            self.engine.log(f"[tray] cannot change autostart: {exc}")
        self.refresh()

    def open_controls(self, icon=None, item=None) -> None:
        if self.panel:
            self.panel.show_controls()

    def open_console(self, icon=None, item=None) -> None:
        if self.panel:
            self.panel.show_console()

    def open_log(self, icon=None, item=None) -> None:
        if os.name == "nt" and os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)                    # noqa: S606 - a file the app itself writes

    def quit(self, icon=None, item=None) -> None:
        threading.Thread(target=self._shutdown, daemon=True).start()

    def stop_lamp(self) -> None:
        """Stops the engine: both lights go off, and the window remembers that the front light is off."""
        if self.panel and self.engine.cfg.front_off_on_exit:
            self.panel.values["front_on"] = False
            self.panel._save()
        self.engine.stop()                            # switches the lights off, takes a moment

    def _shutdown(self) -> None:
        if self.panel:
            self.panel.close()
        self.stop_lamp()
        if self.icon is not None:
            self.icon.stop()

    # --- menu and main loop ----------------------------------------------
    def build_menu(self):
        item = pystray.MenuItem
        return pystray.Menu(
            item("Open", self.open_controls, default=True, visible=False),      # double-click on the icon
            item("Start with Windows", self.toggle_autostart, checked=lambda _: autostart_enabled()),
            item("Debug console", self.open_console),
            item("Open log", self.open_log),
            item("Quit", self.quit),
        )

    def run(self) -> None:
        self.icon = pystray.Icon(APP_NAME, make_icon(), "Yeelight Ambilight", self.build_menu())
        self.icon.run()                               # blocks until icon.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Yeelight Ambilight tray application {VERSION}")
    parser.add_argument("--ip", help="lamp IP (default: auto-discovery)")
    parser.add_argument("--config", help="JSON file with settings")
    parser.add_argument("--monitor", type=int)
    parser.add_argument("--send-fps", type=int)
    parser.add_argument("--capture-fps", type=int)
    parser.add_argument("--bright-max", type=int)
    parser.add_argument("--main-light", choices=["leave", "on", "off"])
    parser.add_argument("--debug", action="store_true", help="write statistics to the log every 2 s")
    args = parser.parse_args()

    if pystray is None:
        fatal("The packages for the tray application are missing.\n\nRun:  pip install -r requirements.txt")
    if already_running():
        message_box("Yeelight Ambilight is already running (see the icon in the notification area).")
        return
    try:
        cfg = load_config(args)
    except SystemExit as exc:                          # load_config reports problems this way
        fatal(f"Settings problem: {exc.code}")
    except (OSError, ValueError) as exc:
        fatal(f"Cannot read the settings file: {exc}")
    cfg.poll_state = args.debug

    log = FileLog(LOG_FILE)
    log(f"=== Yeelight Ambilight tray {VERSION} ===")
    engine = Engine(cfg, log=log)
    engine.debug = args.debug
    app = TrayApp(engine, args.config)
    panel = Panel(engine, SETTINGS_FILE, on_change=app.refresh)
    app.panel = panel
    engine.status_cb = app.on_status
    engine.start()
    SessionEndWatcher(on_ending=app.stop_lamp,
                      on_cancelled=lambda: (engine.start(), app.refresh()),
                      log=log).start()
    app.run()


if __name__ == "__main__":
    main()
