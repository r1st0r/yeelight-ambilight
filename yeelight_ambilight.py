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

# Realtime UDP channel
YEELIGHT_UDP_PORT = 55444

# TCP control channel for set_segment_rgb
YEELIGHT_TCP_PORT = 55443

# ------------------------------------------------------------
# Screen capture
# ------------------------------------------------------------

TOP_PERCENT = 0.15

# Capture rate.
# The lamp's UDP path is verified around 18-20 Hz.
FPS = 30

# ------------------------------------------------------------
# Color processing
# ------------------------------------------------------------

# Maximum output brightness.
MAX_BRIGHTNESS = 0.60

# Treat very dark colors as black.
BLACK_THRESHOLD = 7

# Temporal smoothing.
#
# Higher = faster response
# Lower  = smoother response
#
# 1.0 = no smoothing.
BASE_SMOOTHING = 0.85

# How much a very fast color change should reduce smoothing.
FAST_CHANGE_SMOOTHING = 0.98

# Maximum RGB difference considered "fast".
FAST_CHANGE_THRESHOLD = 70

# ------------------------------------------------------------
# Sampling
# ------------------------------------------------------------

# Instead of averaging every pixel in the whole region,
# we sample several horizontal strips.
#
# This makes the result less sensitive to large areas of
# neutral/white UI and gives faster response to color changes.
SAMPLE_ROWS = 7

# ------------------------------------------------------------
# L/R segmentation
# ------------------------------------------------------------

# The lamp's set_segment_rgb command is TCP based.
#
# We deliberately DO NOT try to send it at 30 FPS because
# lamp15 has a TCP command rate limit.
#
# 8 updates/sec is reasonably responsive while staying below
# the documented ~10 cmd/sec area.
SEGMENT_FPS = 8

# Center overlap between left/right screen zones.
# This prevents a hard vertical seam from causing unstable
# colors right at the monitor center.
SEGMENT_OVERLAP = 0.10

# ------------------------------------------------------------
# Keepalive
# ------------------------------------------------------------

KEEPALIVE_INTERVAL = 8.0


# ============================================================
# Helpers
# ============================================================

RGB = Tuple[int, int, int]


def rgb_to_int(rgb: RGB) -> int:
    """Convert RGB tuple to 0xRRGGBB integer."""

    r, g, b = rgb

    return (
        (r << 16)
        | (g << 8)
        | b
    )


def clamp_channel(value: float) -> int:
    return max(
        0,
        min(
            255,
            int(value),
        ),
    )


def apply_brightness(
    rgb: RGB,
    multiplier: float,
) -> RGB:
    """
    Apply brightness multiplier.

    Important:
    We use RGB magnitude here because set_segment_rgb
    normalizes RGB internally and brightness is controlled
    separately by the lamp. For the current implementation,
    this still keeps our visual output conservative.
    """

    return (
        clamp_channel(rgb[0] * multiplier),
        clamp_channel(rgb[1] * multiplier),
        clamp_channel(rgb[2] * multiplier),
    )


def color_brightness(rgb: RGB) -> int:
    """Simple fast brightness estimate."""

    return max(rgb)


def color_distance(
    a: RGB,
    b: RGB,
) -> float:
    """
    Euclidean RGB distance.

    Used to detect rapid scene/color changes.
    """

    return (
        (
            (a[0] - b[0]) ** 2
            + (a[1] - b[1]) ** 2
            + (a[2] - b[2]) ** 2
        )
        ** 0.5
    )


def adaptive_smoothing(
    previous: Optional[RGB],
    current: RGB,
) -> float:
    """
    Adaptive smoothing.

    Slow changes:
        stronger smoothing -> visually pleasant

    Fast changes:
        almost no smoothing -> avoids lag
    """

    if previous is None:
        return 1.0

    distance = color_distance(
        previous,
        current,
    )

    if distance >= FAST_CHANGE_THRESHOLD:
        return FAST_CHANGE_SMOOTHING

    ratio = (
        distance
        / FAST_CHANGE_THRESHOLD
    )

    return (
        BASE_SMOOTHING
        + (
            FAST_CHANGE_SMOOTHING
            - BASE_SMOOTHING
        )
        * ratio
    )


def smooth_color(
    previous: Optional[RGB],
    current: RGB,
) -> RGB:
    """
    Smooth current color using adaptive smoothing.
    """

    if previous is None:
        return current

    amount = adaptive_smoothing(
        previous,
        current,
    )

    return (
        clamp_channel(
            previous[0]
            + (
                current[0]
                - previous[0]
            )
            * amount
        ),
        clamp_channel(
            previous[1]
            + (
                current[1]
                - previous[1]
            )
            * amount
        ),
        clamp_channel(
            previous[2]
            + (
                current[2]
                - previous[2]
            )
            * amount
        ),
    )


# ============================================================
# Screen sampling
# ============================================================

def average_pixels(
    pixels,
) -> RGB:
    """Average RGB pixels."""

    if not pixels:
        return (0, 0, 0)

    r = 0
    g = 0
    b = 0

    count = 0

    for pixel in pixels:
        r += pixel[0]
        g += pixel[1]
        b += pixel[2]
        count += 1

    if count == 0:
        return (0, 0, 0)

    return (
        r // count,
        g // count,
        b // count,
    )


def sample_region(
    image: Image.Image,
    x1: int,
    x2: int,
) -> RGB:
    """
    Sample several horizontal strips from a screen region.

    We deliberately avoid scanning the entire image repeatedly.
    """

    width, height = image.size

    x1 = max(
        0,
        min(width, x1),
    )

    x2 = max(
        0,
        min(width, x2),
    )

    if x2 <= x1:
        return (0, 0, 0)

    all_pixels = []

    for row in range(SAMPLE_ROWS):

        y = int(
            height
            * (
                (row + 0.5)
                / SAMPLE_ROWS
            )
        )

        # Take one horizontal slice.
        slice_height = max(
            1,
            height // SAMPLE_ROWS,
        )

        crop = image.crop(
            (
                x1,
                max(0, y - slice_height // 2),
                x2,
                min(
                    height,
                    y + slice_height // 2 + 1,
                ),
            )
        )

        all_pixels.extend(
            crop.getdata()
        )

    return average_pixels(
        all_pixels
    )


def capture_screen_colors(
    sct: mss.mss,
) -> Tuple[RGB, RGB]:
    """
    Capture top section of primary monitor.

    Returns:
        left RGB
        right RGB
    """

    monitor = sct.monitors[1]

    width = monitor["width"]
    height = monitor["height"]

    top_height = max(
        1,
        int(
            height
            * TOP_PERCENT
        ),
    )

    screenshot = sct.grab(
        {
            "left": monitor["left"],
            "top": monitor["top"],
            "width": width,
            "height": top_height,
        }
    )

    image = Image.frombytes(
        "RGB",
        screenshot.size,
        screenshot.rgb,
    )

    center = width // 2

    overlap = int(
        width
        * SEGMENT_OVERLAP
        / 2
    )

    left_x1 = 0
    left_x2 = center + overlap

    right_x1 = center - overlap
    right_x2 = width

    left_color = sample_region(
        image,
        left_x1,
        left_x2,
    )

    right_color = sample_region(
        image,
        right_x1,
        right_x2,
    )

    return (
        left_color,
        right_color,
    )


# ============================================================
# UDP realtime protocol
# ============================================================

def get_udp_token(
    sock: socket.socket,
) -> str:
    """
    Start lamp15 UDP session.

    Supports both known response formats.
    """

    request = {
        "id": 1,
        "method": "udp_sess_new",
        "params": [],
    }

    payload = (
        json.dumps(
            request,
            separators=(",", ":"),
        )
        + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (
            YEELIGHT_IP,
            YEELIGHT_UDP_PORT,
        ),
    )

    sock.settimeout(2.0)

    data, _ = sock.recvfrom(
        4096
    )

    response = json.loads(
        data.decode()
    )

    result = response.get(
        "result"
    )

    if (
        isinstance(result, list)
        and result
        and result[0]
    ):
        return result[0]

    params = response.get(
        "params"
    )

    if isinstance(
        params,
        dict,
    ):
        token = params.get(
            "token"
        )

        if token:
            return token

    raise RuntimeError(
        f"Failed to get UDP token: {response}"
    )


def send_udp_command(
    sock: socket.socket,
    token: str,
    method: str,
    params: list,
    command_id: int = 2,
) -> None:
    """Send a realtime UDP command."""

    command = {
        "id": command_id,
        "method": method,
        "params": params,
        "token": token,
    }

    payload = (
        json.dumps(
            command,
            separators=(",", ":"),
        )
        + "\r\n"
    ).encode()

    sock.sendto(
        payload,
        (
            YEELIGHT_IP,
            YEELIGHT_UDP_PORT,
        ),
    )


def send_udp_rgb(
    sock: socket.socket,
    token: str,
    rgb: RGB,
) -> None:
    """
    Realtime background RGB.

    This controls both rear sides together.
    It is NOT used for L/R mode.
    """

    send_udp_command(
        sock,
        token,
        "bg_set_rgb",
        [
            rgb_to_int(rgb),
            "sudden",
            0,
        ],
        2,
    )


def send_udp_keepalive(
    sock: socket.socket,
    token: str,
) -> None:
    """Keep realtime UDP session alive."""

    send_udp_command(
        sock,
        token,
        "udp_sess_keep_alive",
        [
            "keeplive_interval",
            "10",
        ],
        4,
    )


# ============================================================
# TCP segment control
# ============================================================

class SegmentController:
    """
    TCP controller for lamp15 set_segment_rgb.

    This is intentionally rate limited.
    """

    def __init__(self):
        self.sock: Optional[
            socket.socket
        ] = None

        self.last_send = 0.0

        self.interval = (
            1.0 / SEGMENT_FPS
        )

        self.request_id = 100

    def connect(self) -> None:

        if self.sock is not None:
            return

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        sock.settimeout(0.25)

        sock.connect(
            (
                YEELIGHT_IP,
                YEELIGHT_TCP_PORT,
            )
        )

        self.sock = sock

    def close(self) -> None:

        if self.sock is not None:

            try:
                self.sock.close()
            except Exception:
                pass

            self.sock = None

    def send(
        self,
        left: RGB,
        right: RGB,
        force: bool = False,
    ) -> bool:
        """
        Send set_segment_rgb if rate limiter allows.

        Returns:
            True  if command was sent
            False if skipped
        """

        now = time.monotonic()

        if (
            not force
            and (
                now - self.last_send
                < self.interval
            )
        ):
            return False

        self.connect()

        left_int = rgb_to_int(
            left
        )

        right_int = rgb_to_int(
            right
        )

        command = {
            "id": self.request_id,
            "method": "set_segment_rgb",
            "params": [
                left_int,
                right_int,
            ],
        }

        self.request_id += 1

        payload = (
            json.dumps(
                command,
                separators=(",", ":"),
            )
            + "\r\n"
        ).encode()

        try:

            self.sock.sendall(
                payload
            )

            # Read response if available.
            #
            # We don't block waiting for it.
            self.last_send = now

            return True

        except (
            OSError,
            ConnectionError,
            socket.timeout,
        ):

            self.close()

            return False


# ============================================================
# Main
# ============================================================

def main() -> None:

    print("=" * 55)
    print("Yeelight Ambilight v0.3.0")
    print("=" * 55)
    print(
        f"Yeelight IP: {YEELIGHT_IP}"
    )
    print(
        f"UDP port: {YEELIGHT_UDP_PORT}"
    )
    print(
        f"TCP segment FPS: {SEGMENT_FPS}"
    )
    print(
        f"Screen area: top "
        f"{TOP_PERCENT * 100:.1f}%"
    )
    print(
        f"Capture FPS: {FPS}"
    )
    print(
        f"Max brightness: "
        f"{MAX_BRIGHTNESS}"
    )
    print()
    print(
        "Realtime mode:"
    )
    print(
        "  - UDP background realtime"
    )
    print(
        "  - TCP independent L/R segments"
    )
    print(
        "  - RGB=0 for instant black"
    )
    print(
        "  - bg_power OFF only on exit"
    )
    print()
    print(
        "Press Ctrl+C to stop."
    )
    print()

    udp_sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
    )

    segment_controller = (
        SegmentController()
    )

    try:

        # ----------------------------------------------------
        # UDP session
        # ----------------------------------------------------

        token = get_udp_token(
            udp_sock
        )

        print(
            f"UDP session: {token}"
        )

        # ----------------------------------------------------
        # Screen
        # ----------------------------------------------------

        with mss.mss() as sct:

            monitor = sct.monitors[1]

            print(
                "Screen: "
                f"{monitor['width']}x"
                f"{monitor['height']}"
            )

            print()

            previous_left: Optional[
                RGB
            ] = None

            previous_right: Optional[
                RGB
            ] = None

            last_keepalive = (
                time.monotonic()
            )

            last_segment_left: Optional[
                RGB
            ] = None

            last_segment_right: Optional[
                RGB
            ] = None

            frame_time = (
                1.0 / FPS
            )

            while True:

                frame_start = (
                    time.monotonic()
                )

                # --------------------------------------------
                # Capture
                # --------------------------------------------

                (
                    left_color,
                    right_color,
                ) = capture_screen_colors(
                    sct
                )

                # --------------------------------------------
                # Black detection
                #
                # IMPORTANT:
                # We do NOT use bg_power off here.
                #
                # The previous version had:
                #
                #   RGB black
                #       ↓
                #   power OFF
                #       ↓
                #   power ON
                #
                # which caused the visible white/black/white
                # delay.
                #
                # Instead, black is represented by RGB(0,0,0).
                # --------------------------------------------

                overall_brightness = max(
                    color_brightness(
                        left_color
                    ),
                    color_brightness(
                        right_color
                    ),
                )

                if (
                    overall_brightness
                    <= BLACK_THRESHOLD
                ):

                    black = (
                        0,
                        0,
                        0,
                    )

                    # UDP side:
                    # send black immediately.
                    send_udp_rgb(
                        udp_sock,
                        token,
                        black,
                    )

                    # Segment side:
                    #
                    # set_segment_rgb with 0x000000 is
                    # documented as undefined, so DO NOT
                    # send zero to the segment API.
                    #
                    # Instead we keep the last valid segment
                    # color. The UDP background state is the
                    # primary realtime black path.
                    #
                    # This is intentionally not calling
                    # bg_power off/on.
                    previous_left = black
                    previous_right = black

                else:

                    # ----------------------------------------
                    # Apply brightness
                    # ----------------------------------------

                    left_target = (
                        apply_brightness(
                            left_color,
                            MAX_BRIGHTNESS,
                        )
                    )

                    right_target = (
                        apply_brightness(
                            right_color,
                            MAX_BRIGHTNESS,
                        )
                    )

                    # ----------------------------------------
                    # Adaptive smoothing independently for L/R
                    # ----------------------------------------

                    left_output = (
                        smooth_color(
                            previous_left,
                            left_target,
                        )
                    )

                    right_output = (
                        smooth_color(
                            previous_right,
                            right_target,
                        )
                    )

                    previous_left = (
                        left_output
                    )

                    previous_right = (
                        right_output
                    )

                    # ----------------------------------------
                    # L/R realtime
                    # ----------------------------------------
                    #
                    # IMPORTANT:
                    # set_segment_rgb is TCP and rate-limited.
                    # Therefore it cannot run at 30 FPS.
                    #
                    # It updates at SEGMENT_FPS while the screen
                    # capture continues at FPS.
                    # ----------------------------------------

                    segment_sent = (
                        segment_controller.send(
                            left_output,
                            right_output,
                        )
                    )

                    if segment_sent:

                        last_segment_left = (
                            left_output
                        )

                        last_segment_right = (
                            right_output
                        )

                # --------------------------------------------
                # UDP keepalive
                # --------------------------------------------

                now = time.monotonic()

                if (
                    now - last_keepalive
                    >= KEEPALIVE_INTERVAL
                ):

                    send_udp_keepalive(
                        udp_sock,
                        token,
                    )

                    last_keepalive = now

                # --------------------------------------------
                # Frame limiter
                # --------------------------------------------

                elapsed = (
                    time.monotonic()
                    - frame_start
                )

                sleep_time = (
                    frame_time
                    - elapsed
                )

                if sleep_time > 0:

                    time.sleep(
                        sleep_time
                    )

    except KeyboardInterrupt:

        print()
        print(
            "Stopping..."
        )

        # --------------------------------------------
        # Real power OFF only when program exits.
        # --------------------------------------------

        try:

            send_udp_command(
                udp_sock,
                token,
                "bg_set_power",
                [
                    "off",
                    "sudden",
                    0,
                ],
                3,
            )

        except Exception:
            pass

    except Exception as exc:

        print()
        print(
            f"ERROR: {exc}"
        )

        raise

    finally:

        segment_controller.close()

        udp_sock.close()

        print(
            "Done."
        )


if __name__ == "__main__":
    main()
