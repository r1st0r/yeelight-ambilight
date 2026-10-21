"""Pictures of the app icon: the lamp miniature (tray) and the blue rounded square (exe, Start menu)."""

from PIL import Image, ImageDraw, ImageFilter


def lamp_layer(big: int = 256) -> Image.Image:
    """The light bar with its glowing edge and clamp on a transparent square (drawn at big x big)."""
    k = big / 256.0
    bar = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bar)
    for y in range(int(44 * k), int(132 * k)):                # brushed metal: light on top, darker below
        shade = int(236 - 80 * (y - 44 * k) / (88 * k))
        draw.line((8 * k, y, 248 * k, y), fill=(shade, shade + 2, shade + 6, 255))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8 * k, 44 * k, 248 * k, 132 * k), radius=30 * k, fill=255)
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    image.paste(bar, (0, 0), mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((32 * k, 100 * k, 224 * k, 118 * k), radius=9 * k, fill=(255, 248, 225, 255))
    draw.rounded_rectangle((92 * k, 132 * k, 164 * k, 220 * k), radius=12 * k, fill=(118, 124, 138, 255))
    draw.rounded_rectangle((92 * k, 132 * k, 164 * k, 156 * k), radius=8 * k, fill=(160, 166, 180, 255))
    return image


def app_icon(size: int = 256) -> Image.Image:
    """A very dark navy rounded square with the lamp inside, a soft glow under the bar."""
    ss = 4
    big = size * ss
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gradient = Image.new("RGB", (1, big))
    for y in range(big):                                      # very dark navy like the app window, slightly lighter at the top
        t = y / (big - 1)
        gradient.putpixel((0, y), (int(13 - 9 * t), int(24 - 17 * t), int(52 - 36 * t)))
    gradient = gradient.resize((big, big))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.225, fill=255)
    tile.paste(gradient, (0, 0), mask)
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((big * 0.12, big * 0.50, big * 0.88, big * 0.92), fill=(70, 110, 230, 80))
    tile = Image.alpha_composite(tile, glow.filter(ImageFilter.GaussianBlur(big * 0.06)))
    lamp = lamp_layer(int(big * 0.74))
    tile.alpha_composite(lamp, (int((big - lamp.width) / 2), int(big * 0.15)))
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.225, outline=(40, 56, 96, 255),
                                           width=max(2, big // 128))          # a faint rim keeps the edge visible on dark taskbars
    tile.putalpha(Image.composite(tile.getchannel("A"), Image.new("L", (big, big), 0), mask))
    return tile.resize((size, size), Image.LANCZOS)


def save_ico(path: str) -> None:
    app_icon(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    import os
    save_ico(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico"))
    print("icon.ico written")
