"""
Yeelight Ambilight - a small color picker dialog: HSV palette, hue bar, hex and R/G/B fields.
"""

import colorsys
import tkinter as tk

from PIL import Image, ImageTk

from winstyle import Dragger, center, round_corners

BG, CARD, LINE, TRACK, TEXT, MUTED = "#05080F", "#0B101D", "#1A2338", "#1E2840", "#E8ECF7", "#A9B3CC"
SV_W, SV_H, HUE_H = 240, 170, 16


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


class ColorPicker:
    """`ColorPicker(parent, "#4F6BFF", on_apply)`: a modal-looking dialog; `on_apply("#RRGGBB")` on Apply."""

    def __init__(self, parent, initial, on_apply, family="Segoe UI"):
        self.on_apply = on_apply
        self.family = family
        r, g, b = (int(initial[i:i + 2], 16) for i in (1, 3, 5))
        self.h, self.s, self.v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        win = self.win = tk.Toplevel(parent)
        win.title("Pick a color")
        win.configure(bg=LINE, padx=1, pady=1)                      # 1 px border around the dark window
        win.resizable(False, False)
        win.overrideredirect(True)                                  # no Windows title bar: our own strip below
        win.attributes("-topmost", True)
        inner = tk.Frame(win, bg=BG)
        inner.pack()
        drag = Dragger(win)
        strip = tk.Frame(inner, bg=BG)
        strip.pack(fill="x")
        title = tk.Label(strip, text="Pick a color", bg=BG, fg=TEXT, font=(family, 11, "bold"), padx=18, pady=10)
        title.pack(side="left")
        close = tk.Label(strip, text="\u2715", bg=BG, fg=MUTED, font=(family, 11), padx=14, pady=10, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: win.destroy())
        close.bind("<Enter>", lambda _e: close.config(bg="#3B1F26", fg="#F87171"))
        close.bind("<Leave>", lambda _e: close.config(bg=BG, fg=MUTED))
        for widget in (strip, title):
            widget.bind("<Button-1>", drag.press)
            widget.bind("<B1-Motion>", drag.move)
        body = tk.Frame(inner, bg=BG, padx=18, pady=0)
        body.pack()
        tk.Frame(inner, bg=BG, height=16).pack()

        self.sv = tk.Canvas(body, width=SV_W, height=SV_H, bg=BG, highlightthickness=0, cursor="crosshair")
        self.sv.grid(row=0, column=0, columnspan=4)
        self.hue = tk.Canvas(body, width=SV_W, height=HUE_H + 8, bg=BG, highlightthickness=0, cursor="hand2")
        self.hue.grid(row=1, column=0, columnspan=4, pady=(10, 12))
        self._sv_item = self.sv.create_image(0, 0, anchor="nw")
        self._hue_photo = self._make_hue_bar()
        self.hue.create_image(0, 4, anchor="nw", image=self._hue_photo)
        self._hue_marker = self.hue.create_rectangle(0, 0, 0, 0, outline=TEXT, width=2)
        self._sv_marker = self.sv.create_oval(0, 0, 0, 0, outline="#FFFFFF", width=2)
        self.sv.bind("<Button-1>", self._sv_pick)
        self.sv.bind("<B1-Motion>", self._sv_pick)
        self.hue.bind("<Button-1>", self._hue_pick)
        self.hue.bind("<B1-Motion>", self._hue_pick)

        self.preview = tk.Frame(body, width=44, height=44, bg=BG, highlightthickness=1, highlightbackground=LINE)
        self.preview.grid(row=2, column=0, rowspan=2, sticky="w", padx=(0, 12))
        self.preview.grid_propagate(False)
        font = (family, 10)
        self.hex_var = tk.StringVar()
        self.rgb_vars = [tk.StringVar() for _ in range(3)]
        tk.Label(body, text="HEX", bg=BG, fg=MUTED, font=font).grid(row=2, column=1, sticky="w")
        hex_entry = self._entry(body, self.hex_var, 9)
        hex_entry.grid(row=3, column=1, sticky="w")
        hex_entry.bind("<Return>", lambda _e: self._from_hex())
        hex_entry.bind("<FocusOut>", lambda _e: self._from_hex())
        box = tk.Frame(body, bg=BG)
        box.grid(row=2, column=2, rowspan=2, columnspan=2, sticky="e", padx=(12, 0))
        for i, name in enumerate("RGB"):
            tk.Label(box, text=name, bg=BG, fg=MUTED, font=font).grid(row=0, column=i)
            entry = self._entry(box, self.rgb_vars[i], 4)
            entry.grid(row=1, column=i, padx=2)
            entry.bind("<Return>", lambda _e: self._from_rgb())
            entry.bind("<FocusOut>", lambda _e: self._from_rgb())

        buttons = tk.Frame(body, bg=BG)
        buttons.grid(row=4, column=0, columnspan=4, sticky="e", pady=(16, 0))
        for text, command, fill, fg in (("Cancel", win.destroy, TRACK, TEXT), ("Apply", self._apply, TEXT, BG)):
            tk.Button(buttons, text=text, command=command, bg=fill, fg=fg, activebackground=fill, activeforeground=fg,
                      bd=0, padx=16, pady=5, font=font, cursor="hand2").pack(side="left", padx=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())
        px = parent.winfo_rootx() + (parent.winfo_width() - 300) // 2
        py = parent.winfo_rooty() + 120
        win.geometry(f"+{max(0, px)}+{max(0, py)}")
        round_corners(win)
        win.after(80, win.focus_force)
        self._sv_photo = None
        self._sv_hue = None
        self._redraw()

    def _entry(self, parent, var, width):
        return tk.Entry(parent, textvariable=var, width=width, bg=TRACK, fg=TEXT, insertbackground=TEXT, bd=0,
                        highlightthickness=1, highlightbackground=LINE, highlightcolor="#8FA8FF",
                        font=(self.family, 10), justify="center")

    # ----- pictures ---------------------------------------------------------------
    def _make_hue_bar(self):
        h = Image.linear_gradient("L").rotate(90).resize((SV_W, HUE_H))           # 0..255 left to right
        full = Image.new("L", (SV_W, HUE_H), 255)
        return ImageTk.PhotoImage(Image.merge("HSV", (h.transpose(Image.FLIP_LEFT_RIGHT), full, full)).convert("RGB"))

    def _make_sv(self):
        hue = Image.new("L", (SV_W, SV_H), int(self.h * 255))
        sat = Image.linear_gradient("L").rotate(90).resize((SV_W, SV_H))
        val = Image.linear_gradient("L").resize((SV_W, SV_H)).transpose(Image.FLIP_TOP_BOTTOM)
        return ImageTk.PhotoImage(Image.merge("HSV", (hue, sat, val)).convert("RGB"))

    # ----- state ------------------------------------------------------------------
    def _rgb(self):
        r, g, b = colorsys.hsv_to_rgb(self.h, self.s, self.v)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def _redraw(self, hue_changed=True):
        if hue_changed or self._sv_hue != round(self.h, 3):
            self._sv_photo = self._make_sv()
            self._sv_hue = round(self.h, 3)
            self.sv.itemconfig(self._sv_item, image=self._sv_photo)
        x, y = self.s * (SV_W - 1), (1 - self.v) * (SV_H - 1)
        self.sv.coords(self._sv_marker, x - 7, y - 7, x + 7, y + 7)
        hx = self.h * (SV_W - 1)
        self.hue.coords(self._hue_marker, hx - 3, 1, hx + 3, HUE_H + 7)
        rgb = self._rgb()
        self.preview.config(bg=_hex(rgb))
        self.hex_var.set(_hex(rgb))
        for var, value in zip(self.rgb_vars, rgb):
            var.set(str(value))

    def _sv_pick(self, event):
        self.s = min(1.0, max(0.0, event.x / (SV_W - 1)))
        self.v = 1.0 - min(1.0, max(0.0, event.y / (SV_H - 1)))
        self._redraw(hue_changed=False)

    def _hue_pick(self, event):
        self.h = min(1.0, max(0.0, event.x / (SV_W - 1)))
        self._redraw()

    def _set_rgb(self, r, g, b):
        self.h, self.s, self.v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        self._redraw()

    def _from_hex(self):
        text = self.hex_var.get().strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        try:
            if len(text) != 6:
                raise ValueError
            self._set_rgb(*(int(text[i:i + 2], 16) for i in (0, 2, 4)))
        except ValueError:
            self._redraw(hue_changed=False)                      # put the valid value back

    def _from_rgb(self):
        try:
            values = [min(255, max(0, int(var.get()))) for var in self.rgb_vars]
            self._set_rgb(*values)
        except ValueError:
            self._redraw(hue_changed=False)

    def _apply(self):
        self.on_apply(_hex(self._rgb()))
        self.win.destroy()
