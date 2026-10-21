"""
Yeelight Ambilight - the glowing lamp picture at the top of the control window.

`render_stage(...)` draws the light bar with the back glow (colors of the left and right half of
the screen) and the front light cone (color temperature, brightness) with Pillow only.
The glows are drawn at half resolution and enlarged, which is cheap and looks the same
because they are blurred anyway.
"""

import base64
import io

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from lamp_png import LAMP_PNG_BASE64

BG = (5, 8, 15)
DESIGN_W, DESIGN_H = 1280, 560
_lamp_cache = {}


def temp_rgb(kelvin: float):
    """Color of the front light for a temperature in kelvin (2700..6500)."""
    warm, mid, cold = (255, 180, 107), (255, 241, 221), (238, 242, 255)
    t = (kelvin - 2700) / 3800.0
    t = min(1.0, max(0.0, t))
    a, b, u = (warm, mid, t / 0.5) if t < 0.5 else (mid, cold, (t - 0.5) / 0.5)
    return tuple(int(round(a[i] + (b[i] - a[i]) * u)) for i in range(3))


def _radial_mask(size, stops):
    """An 'L' mask: an ellipse filling the box, alpha by stops [(position 0..1, alpha 0..1), ...]."""
    w, h = max(1, size[0]), max(1, size[1])
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    steps = 48
    for i in range(steps):                     # from the outside in
        u = 1.0 - i / steps                    # 1 = edge, 0 = center
        alpha = _interp(stops, u)
        inset_x, inset_y = (1 - u) * w / 2, (1 - u) * h / 2
        draw.ellipse((inset_x, inset_y, w - inset_x, h - inset_y), fill=int(255 * alpha))
    return mask


def _interp(stops, u):
    if u <= stops[0][0]:
        return stops[0][1]
    for (p0, a0), (p1, a1) in zip(stops, stops[1:]):
        if u <= p1:
            return a0 + (a1 - a0) * (u - p0) / (p1 - p0) if p1 > p0 else a1
    return stops[-1][1]


def _scale_mask(mask, factor):
    return mask.point(lambda v: int(v * factor))


def _paste_color(base, color, mask, box):
    """Normal alpha blend of a solid or horizontal-gradient color through `mask` at box's corner."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if isinstance(color[0], tuple):                         # (left, right) gradient across the box
        strip = Image.new("RGB", (2, 1))
        strip.putpixel((0, 0), color[0])
        strip.putpixel((1, 0), color[1])
        fill = strip.resize(mask.size, Image.BILINEAR)
    else:
        fill = Image.new("RGB", mask.size, color)
    piece = fill.convert("RGBA")
    piece.putalpha(mask)
    layer.paste(piece, (int(box[0]), int(box[1])))
    return Image.alpha_composite(base, layer)


def _screen_color(base_rgb, color, mask, box, blur):
    """'screen' blend of a colored shape (alpha = mask) onto an RGB base."""
    layer = Image.new("RGB", base_rgb.size, (0, 0, 0))
    tinted = ImageChops.multiply(Image.new("RGB", mask.size, color), Image.merge("RGB", (mask, mask, mask)))
    layer.paste(tinted, (int(box[0]), int(box[1])))
    if blur > 0.3:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return ImageChops.screen(base_rgb, layer)


def _blur_rgba(img, radius):
    return img.filter(ImageFilter.GaussianBlur(radius)) if radius > 0.3 else img


def render_stage(scale, front_on, front_bri, front_temp, back_on, left_rgb, right_rgb):
    """The picture for a window scaled by `scale` (1.0 = 1280 x 560 px)."""
    q = max(0.1, scale * 0.5)                      # internal scale of the glows
    W, H = int(DESIGN_W * q), int(DESIGN_H * q)
    k = lambda v: v * q
    base = Image.new("RGBA", (W, H), BG + (255,))

    # --- back light: two layers tinted with the screen colors ---
    bk = (1.0 if back_on else 0.0)
    if bk > 0:
        pair = (tuple(left_rgb), tuple(right_rgb))
        outer_box = (k(140), k(20), k(1000), k(420))
        outer = _radial_mask((int(outer_box[2]), int(outer_box[3])), [(0, 0.55), (0.55, 0.18), (1, 0.0)])
        outer = _scale_mask(outer, bk)
        layer = _paste_color(Image.new("RGBA", (W, H), (0, 0, 0, 0)), pair, outer, outer_box)
        base = Image.alpha_composite(base, _blur_rgba(layer, k(40)))
        inner_box = (k(330), k(150), k(620), k(140))
        inner = _radial_mask((int(inner_box[2]), int(inner_box[3])), [(0, 0.9), (1, 0.0)])
        inner = _scale_mask(inner, bk)
        layer = _paste_color(Image.new("RGBA", (W, H), (0, 0, 0, 0)), pair, inner, inner_box)
        base = Image.alpha_composite(base, _blur_rgba(layer, k(22)))

    # --- front light: cone and floor spot, screen-blended ---
    rgb = base.convert("RGB")
    fk = (front_bri / 100.0) if front_on else 0.0
    fc = temp_rgb(front_temp)
    if fk > 0:
        cw, ch = int(k(900)), int(k(300))
        cone = Image.new("L", (cw, ch), 0)
        cd = ImageDraw.Draw(cone)
        for y in range(ch):                                       # vertical gradient .42 -> .10 (60%) -> 0
            u = y / max(1, ch - 1)
            alpha = 0.42 + (0.10 - 0.42) * (u / 0.6) if u < 0.6 else 0.10 * (1 - (u - 0.6) / 0.4)
            cd.line((0, y, cw, y), fill=int(255 * alpha * fk))
        clip = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(clip).polygon([(0.2 * cw, 0), (0.8 * cw, 0), (cw, ch), (0, ch)], fill=255)
        cone = ImageChops.multiply(cone, clip)
        rgb = _screen_color(rgb, fc, cone, (k(190), k(255)), k(16))
        spot = _scale_mask(_radial_mask((int(k(800)), int(k(120))), [(0, 0.30), (1, 0.0)]), fk)
        rgb = _screen_color(rgb, fc, spot, (k(240), k(470)), k(30))
    stage = rgb.resize((int(DESIGN_W * scale), int(DESIGN_H * scale)), Image.BILINEAR).convert("RGBA")

    # --- the lamp itself ---
    lamp = _lamp_cache.get("img")
    if lamp is None:
        lamp = Image.open(io.BytesIO(base64.b64decode(LAMP_PNG_BASE64))).convert("RGBA")
        _lamp_cache["img"] = lamp
    lw = int(600 * scale)
    lh = int(lamp.height * lw / lamp.width)
    key = ("scaled", lw)
    if key not in _lamp_cache:
        _lamp_cache[key] = lamp.resize((lw, lh), Image.LANCZOS)
    stage.alpha_composite(_lamp_cache[key], (int(340 * scale), int(230 * scale)))

    # --- the glowing front edge, on top of the lamp ---
    if fk > 0:
        s = scale
        sw, sh = int(550 * s), max(2, int(12 * s))
        strip = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(strip).rounded_rectangle((0, 0, sw - 1, sh - 1), radius=max(1, int(6 * s)), fill=int(242 * fk))
        rgb2 = _screen_color(stage.convert("RGB"), fc, strip, (365 * s, 246 * s), 6 * s)
        stage = rgb2.convert("RGBA")
    return stage.convert("RGB")
