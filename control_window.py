"""
Yeelight Ambilight - the main control window (tkinter canvas, Pillow for the lamp picture).

Layout follows the design file: a title bar with the connection status, the glowing lamp with
the live colors of the back light, and two cards below: Front light (switch, brightness,
color temperature) and Backlight (switch and a live color bar).

Everything is drawn on one canvas in "design pixels" (1280 x 800) multiplied by a scale
factor, so the window also fits small screens.
"""

import time
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from color_picker import ColorPicker
from stage import render_stage, temp_rgb
from winstyle import Dragger, center, round_corners

DESIGN_W, DESIGN_H = 1280, 800
C_BG, C_CARD, C_LINE, C_TRACK = "#05080F", "#0B101D", "#1A2338", "#1E2840"
C_TEXT, C_MUTED, C_DIM, C_OK = "#E8ECF7", "#A9B3CC", "#8E99B5", "#4ADE80"
C_WARN, C_BAD, C_KNOB_OFF, C_RING = "#FBBF24", "#F87171", "#5A6580", "#262A37"
SWATCHES = ["#4F6BFF", "#8B5CF6", "#EC4899", "#F97316", "#22D3EE", "#F4F6FF"]
CARD_H = 244


def _unpack(color_int: int):
    return (color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255


class ControlWindow:
    def __init__(self, panel, root):
        self.panel = panel
        self.engine = panel.engine
        self.root = root
        self.hits = []                        # (x0, y0, x1, y1, kind, data) in canvas pixels
        self.drag = None
        self._stage_sig = None
        self._stage_t = 0.0
        self._stage_photo = None
        self._bar_photo = None
        self._shapes = {}
        self._hover_btn = None
        self._moving = False

        win = self.win = tk.Toplevel(root)
        win.title("Yeelight Ambilight")
        win.configure(bg=C_BG)
        win.resizable(False, False)
        win.overrideredirect(True)                       # no Windows title bar: the app draws its own
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        self.s = s = max(0.45, min(1.0, (sw - 40) / DESIGN_W, (sh - 120) / DESIGN_H))
        self.W, self.H = int(DESIGN_W * s), int(DESIGN_H * s)
        families = set(tkfont.families(win))
        self.family = next((f for f in ("Manrope", "Segoe UI", "Helvetica Neue", "Helvetica") if f in families),
                           "TkDefaultFont")
        self.canvas = tk.Canvas(win, width=self.W, height=self.H, bg=C_BG, highlightthickness=0)
        self.canvas.pack()
        center(win, self.W, self.H)
        self._drag = Dragger(win)
        round_corners(win)
        self._stage_item = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))     # in front on open, then a normal window
        win.after(100, win.focus_force)
        self.refresh()
        self._tick()

    # ----- small drawing helpers ---------------------------------------------
    def S(self, v):
        return v * self.s

    def font(self, size, weight="normal"):
        return tkfont.Font(family=self.family, size=-max(8, int(round(size * self.s))), weight=weight)

    def ctext(self, x, cy, text, fill, fnt):
        """Text centered on (x, cy) by its letters: Tk centers the whole line box, which sits a little high."""
        cap = abs(fnt.cget("size")) * 0.72                       # cap height in pixels, about
        baseline = cy + cap / 2
        return self.canvas.create_text(x, baseline + fnt.metrics("descent"), text=text, anchor="s", fill=fill,
                                       font=fnt, tags="ui")

    def rrect(self, x0, y0, x1, y1, r, fill="", outline="", width=1, tags="ui"):
        """An anti-aliased rounded rectangle (drawn 4x larger with Pillow, then scaled down and cached).
        Tk's own polygons and ovals have jagged edges, so every shape goes through here."""
        w, h = max(2, int(round(x1 - x0))), max(2, int(round(y1 - y0)))
        key = (w, h, round(r, 1), fill, outline, width)
        photo = self._shapes.get(key)
        if photo is None:
            ss = 4
            im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
            ImageDraw.Draw(im).rounded_rectangle(
                (0, 0, w * ss - 1, h * ss - 1), radius=min(r, w / 2, h / 2) * ss, fill=fill or None,
                outline=outline or None, width=max(1, int(round(width * ss))))
            photo = self._shapes[key] = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
        return self.canvas.create_image(int(round(x0)), int(round(y0)), anchor="nw", image=photo, tags=tags)

    def _window_icon(self, kind, cx, cy, color):
        """A 14 px minus or cross, anti-aliased, cached."""
        size = max(8, int(round(self.S(14))))
        key = ("icon", kind, size, color)
        photo = self._shapes.get(key)
        if photo is None:
            ss, big = 6, size * 6
            im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            width = max(2, int(big * 0.11))
            pad = big * 0.1
            if kind == "winmin":
                d.line((pad, big / 2, big - pad, big / 2), fill=color, width=width)
            else:
                d.line((pad, pad, big - pad, big - pad), fill=color, width=width)
                d.line((pad, big - pad, big - pad, pad), fill=color, width=width)
            photo = self._shapes[key] = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
        return self.canvas.create_image(int(round(cx - size / 2)), int(round(cy - size / 2)), anchor="nw",
                                        image=photo, tags="ui")

    def oval(self, x0, y0, x1, y1, fill="", outline="", width=1, tags="ui"):
        return self.rrect(x0, y0, x1, y1, max(x1 - x0, y1 - y0), fill=fill, outline=outline, width=width, tags=tags)

    def gradient_track(self, x0, x1, ty, low, high):
        """A thin rounded bar colored by the white temperature (warm to cold)."""
        w, h = max(2, int(round(x1 - x0))), max(3, int(round(self.S(4))))
        key = ("grad", w, h, low, high)
        photo = self._shapes.get(key)
        if photo is None:
            strip = Image.new("RGB", (w, 1))
            for x in range(w):
                strip.putpixel((x, 0), temp_rgb(low + (high - low) * x / max(1, w - 1)))
            ss = 4
            bar = strip.resize((w * ss, h * ss), Image.NEAREST).convert("RGBA")
            mask = Image.new("L", (w * ss, h * ss), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=h * ss / 2, fill=255)
            bar.putalpha(mask)
            photo = self._shapes[key] = ImageTk.PhotoImage(bar.resize((w, h), Image.LANCZOS))
        return self.canvas.create_image(int(round(x0)), int(round(ty - h / 2)), anchor="nw", image=photo, tags="ui")

    # ----- state ----------------------------------------------------------------
    def _status(self):
        status = self.engine.status
        text, color = {
            "running": ("connected", C_OK),
            "connecting": ("connecting", C_WARN),
            "lamp not found": ("lamp not found", C_BAD),
            "error": ("error, retrying", C_BAD),
            "stopped": ("stopped", C_BAD),
        }.get(status, (status, C_WARN))
        return text, color

    def _colors(self):
        if not self.engine.ambilight_on:
            return (0, 0, 0), (0, 0, 0)
        if self.panel.values["rear_mode"] == "solid":
            k = self.panel.values["solid_bright"] / 100.0
            color = tuple(int(c * k) for c in _unpack(int(self.panel.values["solid_color"][1:], 16)))
            return color, color
        left, right = getattr(self.engine, "last_colors", (0, 0, 0))
        return _unpack(left), _unpack(right)

    def _stage_signature(self):
        v = self.panel.values
        left, right = self._colors()
        q = lambda c: tuple(x // 6 for x in c)
        return (bool(v["front_on"]), v["front_bright"], v["front_ct"], self.engine.ambilight_on, q(left), q(right))

    def update_stage(self, force=False):
        sig = self._stage_signature()
        if not force and sig == self._stage_sig:
            return
        if not force and time.monotonic() - self._stage_t < 0.06:
            return
        self._stage_sig, self._stage_t = sig, time.monotonic()
        v = self.panel.values
        left, right = self._colors()
        image = render_stage(self.s, bool(v["front_on"]), v["front_bright"], v["front_ct"],
                             self.engine.ambilight_on, left, right)
        self._stage_photo = ImageTk.PhotoImage(image)
        self.canvas.itemconfig(self._stage_item, image=self._stage_photo)

    def _tick(self):
        if not self.win.winfo_exists():
            return
        if self.win.state() != "withdrawn":
            if self.drag is None:
                self.refresh()
            else:
                self.update_stage()
        self.win.after(250, self._tick)

    # ----- drawing -----------------------------------------------------------------
    def refresh(self):
        """Redraws the whole interface layer (cheap) and the lamp picture when it changed."""
        self.update_stage()
        c, s, S = self.canvas, self.s, self.S
        c.delete("ui")
        self.hits = []
        v = self.panel.values

        # title bar
        cy = S(36)
        x = S(40)
        for dx0, dy0, dx1, dy1 in ((3, 8, 21, 8), (10, 8, 10, 13), (10, 13, 14, 13), (14, 13, 14, 8),
                                   (6, 4.5, 7, 6), (18, 4.5, 17, 6), (12, 3, 12, 5)):
            k = 22 / 24
            c.create_line(x + S(dx0 * k), cy - S(11) + S(dy0 * k), x + S(dx1 * k), cy - S(11) + S(dy1 * k),
                          fill=C_TEXT, width=max(1, S(1.6)), capstyle="round", tags="ui")
        c.create_text(x + S(34), cy, text="Yeelight Ambilight", anchor="w", fill=C_TEXT,
                      font=self.font(16, "bold"), tags="ui")
        text, color = self._status()
        label = f"Monitor Light Bar Pro · {text}"
        fnt = self.font(13)
        tw = fnt.measure(label)
        pill_w, pill_h = tw + S(14 * 2 + 16), S(36)
        bsize = S(36)                                    # window buttons: minimize (to the tray) and close
        close_x1 = self.W - S(20)
        for kind, bx1 in (("winclose", close_x1), ("winmin", close_x1 - bsize - S(4))):
            bx0 = bx1 - bsize
            hot = (self._hover_btn == kind)
            if hot:
                self.rrect(bx0, cy - bsize / 2, bx1, cy + bsize / 2, S(10),
                           fill="#3B1F26" if kind == "winclose" else C_LINE)
            self._window_icon(kind, (bx0 + bx1) / 2, cy, "#F87171" if hot and kind == "winclose" else C_TEXT)
            self.hits.append((bx0, cy - bsize / 2, bx1, cy + bsize / 2, kind, None))
        px1 = close_x1 - 2 * bsize - S(4) - S(16)
        self.rrect(px1 - pill_w, cy - pill_h / 2, px1, cy + pill_h / 2, pill_h / 2, fill="#0C1220", outline=C_LINE)
        self.oval(px1 - pill_w + S(14), cy - S(4), px1 - pill_w + S(22), cy + S(4), fill=color, outline="",
                      tags="ui")
        c.create_text(px1 - pill_w + S(30), cy, text=label, anchor="w", fill=C_MUTED, font=fnt, tags="ui")

        # cards
        card_w = (DESIGN_W - 80 - 24) / 2
        card_h = CARD_H
        top = DESIGN_H - 40 - card_h
        self._front_card(S(40), S(top), S(40 + card_w), S(top + card_h), v)
        self._back_card(S(40 + card_w + 24), S(top), S(DESIGN_W - 40), S(top + card_h))
        c.create_rectangle(0, 0, self.W - 1, self.H - 1, outline=C_LINE, tags="ui")      # thin window border

    def _card_frame(self, x0, y0, x1, y1, title, subtitle, state, toggle_key):
        S = self.S
        self.rrect(x0, y0, x1, y1, S(18), fill=C_CARD, outline=C_LINE)
        self.canvas.create_text(x0 + S(24), y0 + S(24), text=title, anchor="nw", fill=C_TEXT,
                                font=self.font(16, "bold"), tags="ui")
        self.canvas.create_text(x0 + S(24), y0 + S(46), text=subtitle, anchor="nw", fill=C_DIM,
                                font=self.font(13), tags="ui")
        # switch 52 x 30
        tx1, ty0 = x1 - S(24), y0 + S(24) + S(4)
        tx0, ty1 = tx1 - S(52), ty0 + S(30)
        self.rrect(tx0, ty0, tx1, ty1, S(15), fill=C_TEXT if state else C_TRACK)
        kx = (tx1 - S(3) - S(24)) if state else (tx0 + S(3))
        self.oval(kx, ty0 + S(3), kx + S(24), ty0 + S(27), fill=C_BG if state else C_KNOB_OFF,
                                outline="", tags="ui")
        self.hits.append((tx0, ty0, tx1, ty1, "toggle", toggle_key))

    def _slider(self, x0, x1, y_label, label, value_text, key, low, high, value, gradient=False):
        S, c = self.S, self.canvas
        c.create_text(x0, y_label, text=label, anchor="nw", fill=C_MUTED, font=self.font(13), tags="ui")
        c.create_text(x1, y_label, text=value_text, anchor="ne", fill=C_MUTED, font=self.font(13), tags="ui")
        ty = y_label + S(32)
        if gradient:
            self.gradient_track(x0, x1, ty, low, high)
        else:
            self.rrect(x0, ty - S(2), x1, ty + S(2), S(2), fill=C_TRACK)
        tx = x0 + (value - low) / (high - low) * (x1 - x0)
        self.oval(tx - S(13), ty - S(13), tx + S(13), ty + S(13), fill=C_RING, outline="", tags="ui")
        self.oval(tx - S(9), ty - S(9), tx + S(9), ty + S(9), fill=C_TEXT, outline="", tags="ui")
        self.hits.append((x0 - S(12), ty - S(16), x1 + S(12), ty + S(16), "slider", (key, low, high, x0, x1)))

    def _front_card(self, x0, y0, x1, y1, v):
        S = self.S
        self._card_frame(x0, y0, x1, y1, "Front light", "Desk illumination", bool(v["front_on"]), "front_on")
        sx0, sx1 = x0 + S(24), x1 - S(24)
        self._slider(sx0, sx1, y0 + S(92), "Brightness", f"{v['front_bright']}%", "front_bright",
                     1, 100, v["front_bright"])
        self._slider(sx0, sx1, y0 + S(166), "Color temperature", f"{v['front_ct']} K", "front_ct",
                     2700, 6500, v["front_ct"], gradient=True)

    def _back_card(self, x0, y0, x1, y1):
        S, c = self.S, self.canvas
        v = self.panel.values
        on = self.engine.ambilight_on
        solid = v["rear_mode"] == "solid"
        self._card_frame(x0, y0, x1, y1, "Backlight",
                         "One static color on the wall" if solid else "Wall glow, follows the screen", on, "back_on")

        # mode switch: Ambilight | Solid color
        mx0, my0, mh = x0 + S(24), y0 + S(78), S(34)
        seg_w = S(130)
        self.rrect(mx0, my0, mx0 + 2 * seg_w + S(8), my0 + mh, mh / 2, fill=C_TRACK)
        for i, (key, label) in enumerate((("ambilight", "Ambilight"), ("solid", "Solid color"))):
            sx0 = mx0 + S(4) + i * seg_w
            active = (v["rear_mode"] == key)
            if active:
                self.rrect(sx0, my0 + S(4), sx0 + seg_w, my0 + mh - S(4), (mh - S(8)) / 2, fill=C_TEXT)
            self.ctext(sx0 + seg_w / 2, my0 + mh / 2, label, C_BG if active else C_MUTED, self.font(13, "bold"))
            self.hits.append((sx0, my0, sx0 + seg_w, my0 + mh, "mode", key))

        if not solid:
            c.create_text(x0 + S(24), y0 + S(132), text="Live colors (left and right of the screen)", anchor="nw",
                          fill=C_MUTED, font=self.font(13), tags="ui")
            width, height = int(x1 - x0 - 2 * S(24)), max(6, int(S(18)))
            left, right = self._colors()
            bar = Image.new("RGB", (2, 1))
            bar.putpixel((0, 0), left if on else (30, 38, 58))
            bar.putpixel((1, 0), right if on else (30, 38, 58))
            bar = bar.resize((width, height), Image.BILINEAR).convert("RGBA")
            mask = Image.new("L", (width, height), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=height // 2, fill=255)
            bar.putalpha(mask)
            self._bar_photo = ImageTk.PhotoImage(bar)
            c.create_image(x0 + S(24), y0 + S(156), anchor="nw", image=self._bar_photo, tags="ui")
            c.create_text(x0 + S(24), y0 + S(190),
                          text="The lamp keeps this light at full brightness\nwhile colors stream; it cannot be dimmed.",
                          anchor="nw", fill=C_DIM, font=self.font(13), tags="ui")
            return

        # solid color: swatches, a custom color, brightness
        colors = list(SWATCHES)
        current = v["solid_color"].upper()
        custom = current not in [col.upper() for col in colors]
        if custom:
            colors.append(current)
        cx, cy, r, gap = x0 + S(24) + S(16), y0 + S(146), S(16), S(44)
        for i, col in enumerate(colors):
            px = cx + i * gap
            selected = col.upper() == current
            if selected:
                self.oval(px - r - S(4), cy - r - S(4), px + r + S(4), cy + r + S(4), fill="", outline=C_TEXT,
                              width=max(1, S(2)), tags="ui")
            self.oval(px - r, cy - r, px + r, cy + r, fill=col, outline="", tags="ui")
            self.hits.append((px - r - S(4), cy - r - S(4), px + r + S(4), cy + r + S(4), "swatch", col))
        px = cx + len(colors) * gap                                # the "more colors" button
        self.oval(px - r, cy - r, px + r, cy + r, fill=C_TRACK, outline=C_LINE, tags="ui")
        self.ctext(px, cy, "+", C_TEXT, self.font(18, "bold"))
        self.hits.append((px - r, cy - r, px + r, cy + r, "picker", None))
        self._slider(x0 + S(24), x1 - S(24), y0 + S(176), "Brightness", f"{v['solid_bright']}%", "solid_bright",
                     1, 100, v["solid_bright"])

    # ----- mouse ----------------------------------------------------------------
    def _find(self, x, y):
        for hit in self.hits:
            if hit[0] <= x <= hit[2] and hit[1] <= y <= hit[3]:
                return hit
        return None

    def _hover(self, event):
        hit = self._find(event.x, event.y)
        self.canvas.config(cursor="hand2" if hit else "")
        button = hit[4] if hit and hit[4] in ("winmin", "winclose") else None
        if button != self._hover_btn:
            self._hover_btn = button
            self.refresh()

    def _press(self, event):
        hit = self._find(event.x, event.y)
        if not hit:
            if event.y < self.S(72):                     # the title strip moves the window
                self._moving = True
                self._drag.press(event)
            return
        kind, data = hit[4], hit[5]
        if kind in ("winmin", "winclose"):               # both hide the window; the app lives in the tray
            self._hover_btn = None
            self.win.withdraw()
        elif kind == "toggle":
            if data == "front_on":
                self.panel._toggle_front(not self.panel.values["front_on"])
            else:
                self.panel._toggle_rear(not self.engine.ambilight_on)
            self.refresh()
        elif kind == "mode":
            self.panel.set_mode(data)
            self.refresh()
        elif kind == "swatch":
            self.panel.set_solid_color(data)
            self.refresh()
        elif kind == "picker":
            ColorPicker(self.win, self.panel.values["solid_color"], self._picked, self.family)
        elif kind == "slider":
            self.drag = data
            self._set_from_x(event.x)

    def _picked(self, hex_color):
        self.panel.set_solid_color(hex_color)
        self.refresh()

    def _motion(self, event):
        if self._moving:
            self._drag.move(event)
            return
        if self.drag:
            self._set_from_x(event.x)

    def _release(self, event):
        if self._moving:
            self._moving = False
            return
        if not self.drag:
            return
        key = self.drag[0]
        self._set_from_x(event.x)
        self.drag = None
        apply = {"front_bright": self.engine.set_front_brightness, "front_ct": self.engine.set_front_ct,
                 "solid_bright": self.engine.set_solid_brightness}[key]
        self.panel._commit(key, self.panel.values[key], apply)
        self.refresh()

    def _set_from_x(self, px):
        key, low, high, x0, x1 = self.drag
        frac = min(1.0, max(0.0, (px - x0) / (x1 - x0)))
        value = low + frac * (high - low)
        if key == "front_ct":
            value = int(round(value / 100.0) * 100)             # steps of 100 K like the design
        else:
            value = int(round(value))
        if value != self.panel.values[key]:
            self.panel.values[key] = value
            self.refresh()

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.refresh()
