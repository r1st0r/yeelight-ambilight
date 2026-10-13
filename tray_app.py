#!/usr/bin/env python3
"""
Yeelight Ambilight - tray application (preview, Windows).

Starts the Ambilight engine in the background and puts an icon in the notification area:

    python tray_app.py            (with a console, for the first try)
    pythonw tray_app.py           (no console window)

Menu: Ambilight on/off, front light on/off, start with Windows, open the log, quit.
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

from ambilight_core import BUILD, VERSION, Engine, load_config

APP_NAME = "YeelightAmbilight"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
LOG_FILE = os.path.join(LOG_DIR, "ambilight.log")

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


ICON_COLORS = {
    "running": ((255, 140, 0), (0, 170, 255)),      # the two halves follow the screen
    "paused": ((120, 120, 120), (120, 120, 120)),
    "connecting": ((230, 200, 40), (230, 200, 40)),
    "problem": ((220, 60, 60), (220, 60, 60)),
}


def make_icon(kind: str):
    """A small picture of the light bar: two halves, colored by state."""
    left, right = ICON_COLORS.get(kind, ICON_COLORS["problem"])
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 14, 61, 50), radius=9, fill=(32, 32, 36, 255))
    draw.rectangle((8, 20, 31, 44), fill=left + (255,))
    draw.rectangle((33, 20, 56, 44), fill=right + (255,))
    return image


# ----------------------------------------------------------------------
# The application
# ----------------------------------------------------------------------

class TrayApp:
    def __init__(self, engine, config_path=None):
        self.engine = engine
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

    def icon_kind(self) -> str:
        status = self.engine.status
        if status == "running":
            return "running" if self.engine.ambilight_on else "paused"
        return "connecting" if status == "connecting" else "problem"

    def refresh(self) -> None:
        icon = self.icon
        if icon is None:
            return
        try:
            icon.icon = make_icon(self.icon_kind())
            icon.title = f"Yeelight Ambilight: {self.status_text()}"
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

    def open_log(self, icon=None, item=None) -> None:
        if os.name == "nt" and os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)                    # noqa: S606 - a file the app itself writes

    def quit(self, icon=None, item=None) -> None:
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self) -> None:
        self.engine.stop()                            # switches the rear light off, takes a moment
        if self.icon is not None:
            self.icon.stop()

    # --- menu and main loop ----------------------------------------------
    def build_menu(self):
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda _: f"Yeelight Ambilight {VERSION}", None, enabled=False),
            item(lambda _: f"Status: {self.status_text()}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Ambilight", self.toggle_ambilight, checked=lambda _: self.engine.ambilight_on),
            item("Front light on", self.front_on),
            item("Front light off", self.front_off),
            pystray.Menu.SEPARATOR,
            item("Start with Windows", self.toggle_autostart, checked=lambda _: autostart_enabled()),
            item("Open log", self.open_log),
            item("Quit", self.quit),
        )

    def run(self) -> None:
        self.icon = pystray.Icon(APP_NAME, make_icon(self.icon_kind()),
                                 f"Yeelight Ambilight: {self.status_text()}", self.build_menu())
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
    log(f"=== Yeelight Ambilight tray {VERSION} ({BUILD}) ===")
    engine = Engine(cfg, log=log)
    engine.debug = args.debug
    app = TrayApp(engine, args.config)
    engine.status_cb = app.on_status
    engine.start()
    app.run()


if __name__ == "__main__":
    main()
