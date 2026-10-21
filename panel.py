"""
Yeelight Ambilight - control window and debug console (tkinter).

Runs in its own thread with its own Tk root, so it can be opened from the tray menu
(pystray owns the main thread). Public API: `Panel(engine, settings_path).show_controls()`,
`.show_console()`, `.close()`.

Sliders apply when released, not while dragging: every change pauses the color stream
for about 1.5 s (the lamp ignores such commands while segment colors arrive).
"""

import json
import math
import os
import queue
import threading
import time

try:
    import tkinter as tk
    from tkinter import filedialog, ttk
except ImportError:                      # tkinter missing: the tray still works without windows
    tk = None

DEFAULTS = {"front_bright": 100, "front_ct": 4000, "front_on": True,
            "rear_mode": "ambilight", "solid_color": "#4F6BFF", "solid_bright": 80}


class Panel:
    def __init__(self, engine, settings_path=None, on_change=None):
        self.engine = engine
        self.on_change = on_change
        self.settings_path = settings_path
        self.values = dict(DEFAULTS)
        self._load()
        self._commands = queue.Queue()
        self._thread = None
        self._root = None
        self._controls = None
        self._console = None
        self._console_text = None
        self._console_seen = 0.0
        self._apply_after = {}

    @property
    def available(self) -> bool:
        return tk is not None

    # ----- settings persistence --------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.settings_path, encoding="utf-8") as fh:
                data = json.load(fh)
            for key in ("front_bright", "front_ct", "solid_bright"):
                if isinstance(data.get(key), int):
                    self.values[key] = data[key]
            if isinstance(data.get("front_on"), bool):
                self.values["front_on"] = data["front_on"]
            if data.get("rear_mode") in ("ambilight", "solid"):
                self.values["rear_mode"] = data["rear_mode"]
            color = data.get("solid_color")
            if isinstance(color, str) and len(color) == 7 and color.startswith("#"):
                int(color[1:], 16)                                  # ValueError if it is not a color
                self.values["solid_color"] = color.upper()
        except (OSError, ValueError, TypeError):
            pass
        self.engine.set_rear_mode(self.values["rear_mode"])
        self.engine.set_solid_color(int(self.values["solid_color"][1:], 16))
        self.engine.set_solid_brightness(self.values["solid_bright"])

    def _save(self) -> None:
        if not self.settings_path:
            return
        try:
            with open(self.settings_path, "w", encoding="utf-8") as fh:
                json.dump(self.values, fh)
        except OSError:
            pass

    # ----- thread-safe entry points ----------------------------------------
    def show_controls(self) -> None:
        self._send("controls")

    def show_console(self) -> None:
        self._send("console")

    def close(self) -> None:
        self._send("quit")

    def _send(self, command: str) -> None:
        if tk is None:
            self.engine.log("[ui] tkinter is not available: windows are disabled")
            return
        self._commands.put(command)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="ui", daemon=True)
            self._thread.start()

    # ----- UI thread -------------------------------------------------------
    def _run(self) -> None:
        try:
            self._root = tk.Tk()
            self._root.withdraw()
            self._root.after(100, self._pump)
            self._root.mainloop()
        except Exception as exc:
            self.engine.log(f"[ui] the window thread failed: {exc!r}")
        finally:
            self._root = None
            self._controls = self._console = None

    def _pump(self) -> None:
        root = self._root
        try:
            while True:
                command = self._commands.get_nowait()
                if command == "controls":
                    self._open_controls()
                elif command == "console":
                    self._open_console()
                elif command == "quit":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        self._refresh_console()
        root.after(200, self._pump)

    # ----- controls window -------------------------------------------------
    def _open_controls(self) -> None:
        if self._controls is not None and self._controls.win.winfo_exists():
            self._controls.show()
            return
        from control_window import ControlWindow
        self._controls = ControlWindow(self, self._root)

    def _toggle_rear(self, state: bool) -> None:
        self.engine.set_ambilight(state)
        if self.on_change:
            self.on_change()

    def set_mode(self, mode: str) -> None:
        self.values["rear_mode"] = mode
        self.engine.set_rear_mode(mode)
        self._save()
        if self.on_change:
            self.on_change()

    def set_solid_color(self, hex_color: str) -> None:
        self.values["solid_color"] = hex_color.upper()
        self.engine.set_solid_color(int(hex_color[1:], 16))
        self._save()

    def _toggle_front(self, state: bool) -> None:
        self.values["front_on"] = bool(state)
        self.engine.set_front_power(state)
        self._save()
        if self.on_change:
            self.on_change()

    def _commit(self, key, value, apply) -> None:
        """Wait 300 ms after the last change, then send it: several sliders share one pause."""
        self.values[key] = int(value)
        job = self._apply_after.pop(key, None)
        if job is not None:
            self._root.after_cancel(job)

        def fire():
            self._apply_after.pop(key, None)
            apply(int(value))
            self._save()

        self._apply_after[key] = self._root.after(300, fire)

    # ----- debug console ---------------------------------------------------
    def _open_console(self) -> None:
        if self._console is not None and self._console.winfo_exists():
            self._console.deiconify()
            self._console.lift()
            return
        win = tk.Toplevel(self._root)
        self._console = win
        win.title("Yeelight Ambilight - debug console")
        win.geometry("900x420")
        top = ttk.Frame(win, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        self._status_label = ttk.Label(top, text="")
        self._status_label.pack(side="left")
        ttk.Button(top, text="Save...", command=self._save_log).pack(side="right", padx=(4, 0))
        ttk.Button(top, text="Copy", command=self._copy_log).pack(side="right", padx=4)
        ttk.Button(top, text="Clear", command=self._clear_log).pack(side="right")
        body = ttk.Frame(win, padding=8)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="none", font=("Consolas", 9), state="disabled")
        bar = ttk.Scrollbar(body, command=text.yview)
        text.config(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        self._console_text = text
        self._console_seen = 0.0
        win.protocol("WM_DELETE_WINDOW", lambda: (win.destroy(), setattr(self, "_console", None)))
        self._refresh_console()

    def _refresh_console(self) -> None:
        win, text = self._console, self._console_text
        if win is None or text is None or not win.winfo_exists():
            return
        snap = self.engine.snapshot()
        self._status_label.config(
            text=f"status: {snap['status']}   capture {snap['capture_fps']:.0f} fps   "
                 f"lamp {snap['lamp_fps']:.0f} fps   errors {snap['errors']}   lamp: {snap['lamp_state']}")
        fresh = [(t, m) for t, m in list(self.engine.logs) if t > self._console_seen]
        if not fresh:
            return
        self._console_seen = fresh[-1][0]
        at_end = text.yview()[1] >= 0.999
        text.config(state="normal")
        for stamp, message in fresh:
            text.insert("end", f"{time.strftime('%H:%M:%S', time.localtime(stamp))}  {message}\n")
        lines = int(text.index("end-1c").split(".")[0])
        if lines > 3000:
            text.delete("1.0", f"{lines - 3000}.0")
        text.config(state="disabled")
        if at_end:
            text.see("end")

    def _log_text(self) -> str:
        return self._console_text.get("1.0", "end-1c")

    def _copy_log(self) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(self._log_text())

    def _clear_log(self) -> None:
        self._console_text.config(state="normal")
        self._console_text.delete("1.0", "end")
        self._console_text.config(state="disabled")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(parent=self._console, defaultextension=".txt",
                                            initialfile="ambilight-debug.txt")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._log_text())
