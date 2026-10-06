#!/usr/bin/env python3

import json
import socket
import time

import mss
from PIL import Image


# ============================================================
# Configuration
# ============================================================

YEELIGHT_IP = "192.168.0.228"
YEELIGHT_UDP_PORT = 55444

# Percentage of the screen height sampled from the top.
TOP_PERCENT = 0.15

# Target update rate.
FPS = 20

# Color smoothing.
# Lower = smoother/slower.
# Higher = faster/more reactive.
SMOOTHING = 0.20

# Below this brightness, the backlight is completely OFF.
BLACK_THRESHOLD = 7

# Maximum backlight brightness.
# 0.35 = approximately 35%.
MAX_BRIGHTNESS = 0.35

# Overlap between left/right screen sampling regions.
# This makes the transition between physical left/right zones
# less abrupt.
OVERLAP = 0.12

# How often to refresh the UDP session.
KEEPALIVE_INTERVAL = 8


# ============================================================
# Color helpers
# ============================================================

def rgb_to_int(rgb):
    """Convert (R, G, B) to Yeelight 0xRRGGBB integer."""
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


def color_brightness(rgb):
    """Perceived brightness using standard luminance weights."""
    r, g, b = rgb

    return (
        0.2126 * r
        + 0.7152 * g
        + 0.0722 * b
    )


def smooth_color(old, new):
    """Interpolate old -> new."""
    return tuple(
        old[i] + (new[i] - old[i]) * SMOOTHING
        for i in range(3)
    )


def weighted_average(image):
    """
    Calculate weighted average RGB.

    Brighter pixels have slightly more influence than very dark
    pixels. Extremely dark pixels are ignored.
    """

    pixels = list(image.getdata())

    red = 0.0
    green = 0.0
    blue = 0.0
    total_weight = 0.0

    for r, g, b in pixels:

        brightness = (
            r + g + b
        ) / 3.0

        if brightness < 3:
            continue

        # Give brighter pixels more influence.
        weight = max(
            1.0,
            brightness / 64.0
        )

        red += r * weight
        green += g * weight
        blue += b * weight

        total_weight += weight

    if total_weight == 0:
        return (0, 0, 0)

    return (
        int(red / total_weight),
        int(green / total_weight),
        int(blue / total_weight),
    )


def limit_brightness(rgb):
    """
    Apply black threshold and maximum brightness.

    Returns (0, 0, 0) when the scene is considered black.
    """

    scene_brightness = color_brightness(rgb)

    # Real black threshold.
    if scene_brightness < BLACK_THRESHOLD:
        return (0, 0, 0)

    peak = max(rgb)

    if peak == 0:
        return (0, 0, 0)

    # Desired brightness is capped.
    desired_factor = min(
        MAX_BRIGHTNESS,
        scene_brightness / 255.0
    )

    scale = (
        255.0 * desired_factor
    ) / peak

    return tuple(
        min(
            255,
            int(channel * scale)
        )
        for channel in rgb
    )


# ============================================================
# Yeelight UDP
# ============================================================

def create_socket():
    """Create UDP socket used for realtime control."""

    return socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )


def get_udp_token(sock):
    """
    Request a Yeelight realtime UDP session.
    """

    message = {
        "id": 1,
        "method": "udp_sess_new",
        "params": []
    }

    payload = (
        json.dumps(message)
        + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (
            YEELIGHT_IP,
            YEELIGHT_UDP_PORT
        )
    )

    sock.settimeout(3)

    data, _ = sock.recvfrom(4096)

    response = json.loads(
        data.decode()
    )

    token = response["params"]["token"]

    print(
        "UDP session:",
        token
    )

    return token


def send_background_color(
    sock,
    token,
    rgb
):
    """
    Set realtime background RGB.
    """

    message = {
        "id": 2,
        "method": "bg_set_rgb",
        "params": [
            rgb_to_int(rgb),
            "sudden",
            0
        ],
        "token": token
    }

    payload = (
        json.dumps(message)
        + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (
            YEELIGHT_IP,
            YEELIGHT_UDP_PORT
        )
    )


def background_power(
    sock,
    token,
    state
):
    """
    Real background power control.

    state:
        "on"
        "off"
    """

    message = {
        "id": 3,
        "method": "bg_set_power",
        "params": [
            state,
            "sudden",
            0
        ],
        "token": token
    }

    payload = (
        json.dumps(message)
        + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (
            YEELIGHT_IP,
            YEELIGHT_UDP_PORT
        )
    )


def keep_alive(
    sock,
    token
):
    """
    Keep the realtime UDP session alive.
    """

    message = {
        "id": 999,
        "method": "udp_sess_keep_alive",
        "params": []
    }

    payload = (
        json.dumps(message)
        + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (
            YEELIGHT_IP,
            YEELIGHT_UDP_PORT
        )
    )


# ============================================================
# Screen capture
# ============================================================

def capture_regions(sct):
    """
    Capture the top portion of the primary monitor.

    Returns:
        left_image
        right_image
    """

    monitor = sct.monitors[1]

    screen_width = monitor["width"]
    screen_height = monitor["height"]

    top_height = int(
        screen_height * TOP_PERCENT
    )

    screenshot = sct.grab({
        "left": monitor["left"],
        "top": monitor["top"],
        "width": screen_width,
        "height": top_height
    })

    image = Image.frombytes(
        "RGB",
        screenshot.size,
        screenshot.rgb
    )

    # Left and right sampling regions overlap.
    left_x1 = 0
    left_x2 = int(
        screen_width
        * (0.5 + OVERLAP)
    )

    right_x1 = int(
        screen_width
        * (0.5 - OVERLAP)
    )

    right_x2 = screen_width

    left_image = image.crop((
        left_x1,
        0,
        left_x2,
        top_height
    ))

    right_image = image.crop((
        right_x1,
        0,
        right_x2,
        top_height
    ))

    return (
        left_image,
        right_image
    )


# ============================================================
# Main
# ============================================================

def main():

    print(
        "Yeelight Ambilight v0.2.0"
    )

    print(
        "Screen area:",
        TOP_PERCENT * 100,
        "%"
    )

    print(
        "Target FPS:",
        FPS
    )

    print(
        "Maximum brightness:",
        MAX_BRIGHTNESS * 100,
        "%"
    )

    print(
        "Press Ctrl+C to stop."
    )

    sock = create_socket()

    token = get_udp_token(sock)

    previous_left = (
        0.0,
        0.0,
        0.0
    )

    previous_right = (
        0.0,
        0.0,
        0.0
    )

    backlight_on = False

    last_keepalive = (
        time.monotonic()
    )

    frame_delay = 1.0 / FPS

    with mss.MSS() as sct:

        monitor = sct.monitors[1]

        screen_width = monitor["width"]
        screen_height = monitor["height"]

        top_height = int(
            screen_height * TOP_PERCENT
        )

        print(
            f"Screen: "
            f"{screen_width}x"
            f"{screen_height}"
        )

        print(
            f"Sampling top: "
            f"{top_height}px"
        )

        try:

            while True:

                frame_start = (
                    time.monotonic()
                )

                # --------------------------------------------
                # Capture screen
                # --------------------------------------------

                left_image, right_image = (
                    capture_regions(sct)
                )

                # --------------------------------------------
                # Calculate colors
                # --------------------------------------------

                left_color = (
                    weighted_average(
                        left_image
                    )
                )

                right_color = (
                    weighted_average(
                        right_image
                    )
                )

                # --------------------------------------------
                # Apply brightness limiting
                # --------------------------------------------

                left_color = (
                    limit_brightness(
                        left_color
                    )
                )

                right_color = (
                    limit_brightness(
                        right_color
                    )
                )

                # --------------------------------------------
                # Smooth transitions
                # --------------------------------------------

                previous_left = (
                    smooth_color(
                        previous_left,
                        left_color
                    )
                )

                previous_right = (
                    smooth_color(
                        previous_right,
                        right_color
                    )
                )

                # Convert float -> integer RGB.
                left_output = tuple(
                    max(
                        0,
                        min(
                            255,
                            int(channel)
                        )
                    )
                    for channel in previous_left
                )

                right_output = tuple(
                    max(
                        0,
                        min(
                            255,
                            int(channel)
                        )
                    )
                    for channel in previous_right
                )

                # --------------------------------------------
                # Determine whether the whole scene is black.
                # --------------------------------------------

                combined_brightness = max(
                    color_brightness(
                        left_output
                    ),
                    color_brightness(
                        right_output
                    )
                )

                if (
                    combined_brightness
                    < BLACK_THRESHOLD
                ):

                    if backlight_on:

                        background_power(
                            sock,
                            token,
                            "off"
                        )

                        backlight_on = False

                else:

                    if not backlight_on:

                        background_power(
                            sock,
                            token,
                            "on"
                        )

                        backlight_on = True

                    # ------------------------------------------------
                    # v0.2 currently sends one blended realtime color.
                    #
                    # Independent left/right realtime control will be
                    # added after testing the segment protocol.
                    # ------------------------------------------------

                    blended = tuple(
                        int(
                            (
                                left_output[i]
                                + right_output[i]
                            )
                            / 2
                        )
                        for i in range(3)
                    )

                    send_background_color(
                        sock,
                        token,
                        blended
                    )

                # --------------------------------------------
                # Keep UDP session alive.
                # --------------------------------------------

                now = time.monotonic()

                if (
                    now - last_keepalive
                    > KEEPALIVE_INTERVAL
                ):

                    keep_alive(
                        sock,
                        token
                    )

                    last_keepalive = now

                # --------------------------------------------
                # Maintain FPS.
                # --------------------------------------------

                elapsed = (
                    time.monotonic()
                    - frame_start
                )

                sleep_time = (
                    frame_delay
                    - elapsed
                )

                if sleep_time > 0:
                    time.sleep(
                        sleep_time
                    )

        except KeyboardInterrupt:

            print(
                "\nStopping..."
            )

            # Always leave the backlight OFF
            # when the program exits.
            background_power(
                sock,
                token,
                "off"
            )

            print(
                "Backlight off."
            )

        finally:

            sock.close()


if __name__ == "__main__":
    main()
