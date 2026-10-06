"""
Yeelight Screen Light Bar Pro Ambilight
Version: 0.2.0

Current implementation:
- Windows/macOS screen capture via MSS
- Samples the top part of the display
- Calculates left/right colors separately
- Uses a blended color for the current Yeelight realtime background API
- UDP realtime control on port 55444
- Fast background OFF on black scenes
- Configurable FPS, brightness and threshold

Hardware tested:
- Yeelight Screen Light Bar Pro
- model: lamp15
- firmware: 38

TODO for v0.3:
- True independent realtime left/right control
- Better dominant-color selection
- Improved transition handling
- Windows autostart
"""

import json
import socket
import time
from typing import Optional, Tuple

import mss
from PIL import Image


# ============================================================
# Configuration
# ============================================================

YEELIGHT_IP = "192.168.0.228"
YEELIGHT_UDP_PORT = 55444

# Screen sampling
TOP_PERCENT = 0.15

# Realtime loop
FPS = 30

# 1.0 = no smoothing / minimum latency
SMOOTHING = 1.0

# Black detection
BLACK_THRESHOLD = 7

# Maximum brightness multiplier
MAX_BRIGHTNESS = 0.35

# Left/right sampling overlap
OVERLAP = 0.12

# UDP keepalive
KEEPALIVE_INTERVAL = 8.0


# ============================================================
# Helpers
# ============================================================

def rgb_to_int(rgb: Tuple[int, int, int]) -> int:
    """Convert RGB tuple to Yeelight integer RGB."""
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


def apply_brightness(
    rgb: Tuple[int, int, int],
    multiplier: float,
) -> Tuple[int, int, int]:
    """Apply brightness multiplier to RGB."""
    r, g, b = rgb

    return (
        min(255, int(r * multiplier)),
        min(255, int(g * multiplier)),
        min(255, int(b * multiplier)),
    )


def color_brightness(rgb: Tuple[int, int, int]) -> int:
    """Simple brightness estimate."""
    r, g, b = rgb
    return max(r, g, b)


def smooth_color(
    previous: Optional[Tuple[int, int, int]],
    current: Tuple[int, int, int],
    amount: float,
) -> Tuple[int, int, int]:
    """
    Smooth transition between colors.

    amount = 1.0 -> immediate change
    amount = 0.2 -> strong smoothing
    """
    if previous is None:
        return current

    if amount >= 1.0:
        return current

    r = int(previous[0] + (current[0] - previous[0]) * amount)
    g = int(previous[1] + (current[1] - previous[1]) * amount)
    b = int(previous[2] + (current[2] - previous[2]) * amount)

    return r, g, b


def average_region(image: Image.Image) -> Tuple[int, int, int]:
    """Calculate average RGB value of an image region."""
    pixels = list(image.getdata())

    if not pixels:
        return 0, 0, 0

    r = sum(pixel[0] for pixel in pixels)
    g = sum(pixel[1] for pixel in pixels)
    b = sum(pixel[2] for pixel in pixels)

    count = len(pixels)

    return (
        r // count,
        g // count,
        b // count,
    )


# ============================================================
# Yeelight UDP
# ============================================================

def get_udp_token(sock: socket.socket) -> str:
    """Create a realtime UDP session and return its token."""

    request = {
        "id": 1,
        "method": "udp_sess_new",
        "params": [],
    }

    payload = (
        json.dumps(request, separators=(",", ":")) + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (YEELIGHT_IP, YEELIGHT_UDP_PORT),
    )

    sock.settimeout(2.0)

    data, _ = sock.recvfrom(4096)

    response = json.loads(data.decode())

    token = response.get("result")

    if not token:
        raise RuntimeError(
            f"Failed to get UDP token: {response}"
        )

    return token[0]


def send_background_color(
    sock: socket.socket,
    token: str,
    rgb: Tuple[int, int, int],
) -> None:
    """Send realtime background RGB."""

    command = {
        "id": 2,
        "method": "bg_set_rgb",
        "params": [
            rgb_to_int(rgb),
            "sudden",
            0,
        ],
        "token": token,
    }

    payload = (
        json.dumps(command, separators=(",", ":")) + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (YEELIGHT_IP, YEELIGHT_UDP_PORT),
    )


def background_power(
    sock: socket.socket,
    token: str,
    power: str,
) -> None:
    """Turn background light on/off."""

    command = {
        "id": 3,
        "method": "bg_set_power",
        "params": [
            power,
            "sudden",
            0,
        ],
        "token": token,
    }

    payload = (
        json.dumps(command, separators=(",", ":")) + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (YEELIGHT_IP, YEELIGHT_UDP_PORT),
    )


def keep_alive(
    sock: socket.socket,
    token: str,
) -> None:
    """Keep realtime UDP session alive."""

    command = {
        "id": 4,
        "method": "udp_sess_keep_alive",
        "params": [],
        "token": token,
    }

    payload = (
        json.dumps(command, separators=(",", ":")) + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (YEELIGHT_IP, YEELIGHT_UDP_PORT),
    )


# ============================================================
# Screen capture
# ============================================================

def capture_regions(
    sct: mss.mss,
) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """
    Capture the top portion of the primary monitor.

    Returns:
        left_color
        right_color
    """

    monitor = sct.monitors[1]

    width = monitor["width"]
    height = monitor["height"]

    sample_height = max(
        1,
        int(height * TOP_PERCENT),
    )

    screenshot = sct.grab(
        {
            "left": monitor["left"],
            "top": monitor["top"],
            "width": width,
            "height": sample_height,
        }
    )

    image = Image.frombytes(
        "RGB",
        screenshot.size,
        screenshot.rgb,
    )

    overlap_pixels = int(width * OVERLAP)

    center = width // 2

    left_end = min(
        width,
        center + overlap_pixels,
    )

    right_start = max(
        0,
        center - overlap_pixels,
    )

    left_image = image.crop(
        (
            0,
            0,
            left_end,
            sample_height,
        )
    )

    right_image = image.crop(
        (
            right_start,
            0,
            width,
            sample_height,
        )
    )

    left_color = average_region(left_image)
    right_color = average_region(right_image)

    return left_color, right_color


# ============================================================
# Main
# ============================================================

def main() -> None:
    print("=" * 50)
    print("Yeelight Ambilight v0.2.0")
    print("=" * 50)
    print(f"Yeelight IP: {YEELIGHT_IP}")
    print(f"UDP port: {YEELIGHT_UDP_PORT}")
    print(f"Screen area: top {TOP_PERCENT * 100:.1f}%")
    print(f"FPS: {FPS}")
    print(f"Smoothing: {SMOOTHING}")
    print(f"Max brightness: {MAX_BRIGHTNESS}")
    print()
    print("Press Ctrl+C to stop.")
    print()

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
    )

    token = get_udp_token(sock)

    print(f"UDP session: {token}")

    previous_color: Optional[Tuple[int, int, int]] = None
    light_is_off = False

    last_keepalive = time.monotonic()

    frame_interval = 1.0 / FPS

    with mss.MSS() as sct:

        monitor = sct.monitors[1]

        print(
            f"Screen: "
            f"{monitor['width']}x{monitor['height']}, "
            f"sampling top {int(monitor['height'] * TOP_PERCENT)}px"
        )

        print()

        try:

            while True:

                frame_start = time.monotonic()

                # ------------------------------------------------
                # Capture screen
                # ------------------------------------------------

                left_color, right_color = capture_regions(sct)

                # ------------------------------------------------
                # Current v0.2 behavior:
                # blend left/right into one realtime color.
                #
                # True independent L/R control will come later.
                # ------------------------------------------------

                blended_color = (
                    int((left_color[0] + right_color[0]) / 2),
                    int((left_color[1] + right_color[1]) / 2),
                    int((left_color[2] + right_color[2]) / 2),
                )

                brightness = color_brightness(blended_color)

                # ------------------------------------------------
                # Black scene
                # ------------------------------------------------

                if brightness <= BLACK_THRESHOLD:

                    if not light_is_off:

                        background_power(
                            sock,
                            token,
                            "off",
                        )

                        light_is_off = True
                        previous_color = None

                    # No RGB command while black.
                    # This avoids fighting the OFF state.

                else:

                    # ------------------------------------------------
                    # Wake background light immediately
                    # ------------------------------------------------

                    if light_is_off:

                        background_power(
                            sock,
                            token,
                            "on",
                        )

                        light_is_off = False

                    # ------------------------------------------------
                    # Brightness limiting
                    # ------------------------------------------------

                    target_color = apply_brightness(
                        blended_color,
                        MAX_BRIGHTNESS,
                    )

                    # ------------------------------------------------
                    # Optional smoothing
                    # ------------------------------------------------

                    output_color = smooth_color(
                        previous_color,
                        target_color,
                        SMOOTHING,
                    )

                    previous_color = output_color

                    # ------------------------------------------------
                    # Send realtime color
                    # ------------------------------------------------

                    send_background_color(
                        sock,
                        token,
                        output_color,
                    )

                # ------------------------------------------------
                # Keepalive
                # ------------------------------------------------

                now = time.monotonic()

                if (
                    now - last_keepalive
                    >= KEEPALIVE_INTERVAL
                ):
                    keep_alive(
                        sock,
                        token,
                    )

                    last_keepalive = now

                # ------------------------------------------------
                # FPS limiter
                # ------------------------------------------------

                elapsed = (
                    time.monotonic()
                    - frame_start
                )

                sleep_time = (
                    frame_interval
                    - elapsed
                )

                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:

            print()
            print("Stopping Ambilight...")

            try:
                background_power(
                    sock,
                    token,
                    "off",
                )
            except Exception:
                pass

            print("Background light OFF.")

        finally:

            sock.close()


if __name__ == "__main__":
    main()
