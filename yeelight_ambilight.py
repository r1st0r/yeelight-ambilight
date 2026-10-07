
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

TCP_PORT = 55443
UDP_PORT = 55444

# Screen
TOP_PERCENT = 0.15

# Capture loop
FPS = 30

# Actual background brightness.
#
# IMPORTANT:
# lamp15 set_segment_rgb ignores RGB magnitude.
# Brightness is controlled separately with bg_set_bright.
BG_BRIGHTNESS = 60

# Very dark screen = black.
BLACK_THRESHOLD = 7

# ------------------------------------------------------------
# L/R sampling
# ------------------------------------------------------------

# Slight overlap around the center prevents a hard seam.
CENTER_OVERLAP = 0.08

# Number of horizontal sampling bands.
SAMPLE_ROWS = 7

# ------------------------------------------------------------
# Adaptive smoothing
# ------------------------------------------------------------

# Slow changes are smoothed.
BASE_SMOOTHING = 0.65

# Fast changes almost bypass smoothing.
FAST_SMOOTHING = 0.95

# Distance at which we consider a color change "fast".
FAST_CHANGE_THRESHOLD = 60.0

# ------------------------------------------------------------
# Segment update rate
# ------------------------------------------------------------

# TCP limit is roughly 10 commands/sec.
#
# We stay slightly below it.
SEGMENT_FPS = 9

# ------------------------------------------------------------
# UDP keepalive
# ------------------------------------------------------------

KEEPALIVE_INTERVAL = 8.0


RGB = Tuple[int, int, int]


# ============================================================
# RGB helpers
# ============================================================

def rgb_to_int(rgb: RGB) -> int:
    r, g, b = rgb

    return (
        (r << 16)
        | (g << 8)
        | b
    )


def color_brightness(rgb: RGB) -> int:
    return max(rgb)


def color_distance(
    a: RGB,
    b: RGB,
) -> float:

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

    if previous is None:
        return 1.0

    distance = color_distance(
        previous,
        current,
    )

    if distance >= FAST_CHANGE_THRESHOLD:
        return FAST_SMOOTHING

    ratio = (
        distance
        / FAST_CHANGE_THRESHOLD
    )

    return (
        BASE_SMOOTHING
        + (
            FAST_SMOOTHING
            - BASE_SMOOTHING
        )
        * ratio
    )


def smooth_color(
    previous: Optional[RGB],
    current: RGB,
) -> RGB:

    if previous is None:
        return current

    amount = adaptive_smoothing(
        previous,
        current,
    )

    return (
        int(
            previous[0]
            + (
                current[0]
                - previous[0]
            )
            * amount
        ),
        int(
            previous[1]
            + (
                current[1]
                - previous[1]
            )
            * amount
        ),
        int(
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

    pixels = []

    row_height = max(
        1,
        height // SAMPLE_ROWS,
    )

    for row in range(SAMPLE_ROWS):

        y1 = row * row_height
        y2 = min(
            height,
            y1 + row_height,
        )

        crop = image.crop(
            (
                x1,
                y1,
                x2,
                y2,
            )
        )

        pixels.extend(
            crop.getdata()
        )

    return average_pixels(
        pixels
    )


def capture_screen_colors(
    sct: mss.mss,
) -> Tuple[RGB, RGB]:

    monitor = sct.monitors[1]

    width = monitor["width"]
    height = monitor["height"]

    capture_height = max(
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
            "height": capture_height,
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
        * CENTER_OVERLAP
        / 2
    )

    left_color = sample_region(
        image,
        0,
        center + overlap,
    )

    right_color = sample_region(
        image,
        center - overlap,
        width,
    )

    return (
        left_color,
        right_color,
    )


# ============================================================
# TCP Yeelight
# ============================================================

class YeelightTCP:

    def __init__(self):
        self.sock: Optional[
            socket.socket
        ] = None

        self.request_id = 1

        self.last_segment_send = 0.0

        self.segment_interval = (
            1.0 / SEGMENT_FPS
        )

    def connect(self):

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
                TCP_PORT,
            )
        )

        self.sock = sock

    def close(self):

        if self.sock is not None:

            try:
                self.sock.close()
            except Exception:
                pass

        self.sock = None

    def command(
        self,
        method: str,
        params: list,
        wait_response: bool = True,
    ):

        self.connect()

        request_id = self.request_id

        self.request_id += 1

        request = {
            "id": request_id,
            "method": method,
            "params": params,
        }

        payload = (
            json.dumps(
                request,
                separators=(",", ":"),
            )
            + "\r\n"
        ).encode()

        try:

            self.sock.sendall(
                payload
            )

            if not wait_response:
                return None

            # Read exactly one response.
            #
            # This is important because otherwise TCP responses
            # accumulate in the socket while we keep sending
            # commands.
            response = (
                self.sock.recv(4096)
            )

            if not response:
                raise ConnectionError(
                    "Yeelight closed TCP connection"
                )

            return json.loads(
                response.decode()
            )

        except (
            OSError,
            ConnectionError,
            socket.timeout,
            json.JSONDecodeError,
        ):

            self.close()

            raise

    def set_background_brightness(
        self,
        brightness: int,
    ):

        brightness = max(
            1,
            min(
                100,
                brightness,
            ),
        )

        self.command(
            "bg_set_bright",
            [
                brightness,
                "sudden",
                0,
            ],
            wait_response=True,
        )

    def background_power(
        self,
        power: str,
    ):

        self.command(
            "bg_set_power",
            [
                power,
                "sudden",
                0,
            ],
            wait_response=True,
        )

    def set_segment_rgb(
        self,
        left: RGB,
        right: RGB,
        force: bool = False,
    ) -> bool:

        now = time.monotonic()

        if (
            not force
            and (
                now
                - self.last_segment_send
                < self.segment_interval
            )
        ):
            return False

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # RGB magnitude is intentionally NOT brightness-scaled.
        #
        # On lamp15:
        #   0xFF0000 = red
        #   0x010000 = also red
        #
        # Brightness is controlled separately by bg_set_bright.
        # ----------------------------------------------------

        request = {
            "id": self.request_id,
            "method": "set_segment_rgb",
            "params": [
                rgb_to_int(left),
                rgb_to_int(right),
            ],
        }

        self.request_id += 1

        payload = (
            json.dumps(
                request,
                separators=(",", ":"),
            )
            + "\r\n"
        ).encode()

        try:

            self.connect()

            self.sock.sendall(
                payload
            )

            # We MUST consume the response.
            #
            # Otherwise responses accumulate and the TCP stream
            # eventually becomes desynchronized.
            self.sock.recv(4096)

            self.last_segment_send = now

            return True

        except (
            OSError,
            ConnectionError,
            socket.timeout,
        ):

            self.close()

            return False


# ============================================================
# UDP realtime
# ============================================================

class YeelightUDP:

    def __init__(self):

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.sock.settimeout(2.0)

        self.token: Optional[str] = None

    def connect(self):

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

        self.sock.sendto(
            payload,
            (
                YEELIGHT_IP,
                UDP_PORT,
            ),
        )

        data, _ = self.sock.recvfrom(
            4096
        )

        response = json.loads(
            data.decode()
        )

        params = response.get(
            "params"
        )

        if (
            isinstance(params, dict)
            and params.get("token")
        ):
            self.token = params["token"]

            return self.token

        result = response.get(
            "result"
        )

        if (
            isinstance(result, list)
            and result
        ):
            self.token = result[0]

            return self.token

        raise RuntimeError(
            f"Could not obtain UDP token: "
            f"{response}"
        )

    def command(
        self,
        method: str,
        params: list,
        command_id: int = 2,
    ):

        if not self.token:
            raise RuntimeError(
                "UDP session is not initialized"
            )

        request = {
            "id": command_id,
            "method": method,
            "params": params,
            "token": self.token,
        }

        payload = (
            json.dumps(
                request,
                separators=(",", ":"),
            )
            + "\r\n"
        ).encode()

        self.sock.sendto(
            payload,
            (
                YEELIGHT_IP,
                UDP_PORT,
            ),
        )

    def keepalive(self):

        self.command(
            "udp_sess_keep_alive",
            [
                "keeplive_interval",
                "10",
            ],
            4,
        )

    def close(self):

        try:
            self.sock.close()
        except Exception:
            pass


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 55)
    print("Yeelight Ambilight v0.3.1")
    print("=" * 55)

    print(
        f"Yeelight: {YEELIGHT_IP}"
    )

    print(
        f"Screen: top "
        f"{TOP_PERCENT * 100:.1f}%"
    )

    print(
        f"Capture FPS: {FPS}"
    )

    print(
        f"Segment FPS: {SEGMENT_FPS}"
    )

    print(
        f"Background brightness: "
        f"{BG_BRIGHTNESS}%"
    )

    print()
    print(
        "L/R: set_segment_rgb"
    )

    print(
        "Black: bg_power OFF"
    )

    print()
    print(
        "Press Ctrl+C to stop."
    )

    print()

    udp = YeelightUDP()
    tcp = YeelightTCP()

    try:

        # ----------------------------------------------------
        # UDP session
        # ----------------------------------------------------

        token = udp.connect()

        print(
            f"UDP session: {token}"
        )

        # ----------------------------------------------------
        # Configure background brightness ONCE.
        #
        # This controls the actual physical brightness of
        # both L/R segments.
        # ----------------------------------------------------

        print(
            "Setting background brightness..."
        )

        tcp.set_background_brightness(
            BG_BRIGHTNESS
        )

        # ----------------------------------------------------
        # Make sure background is ON before starting.
        # ----------------------------------------------------

        tcp.background_power(
            "on"
        )

        print(
            "Background ON."
        )

        print()

        # ----------------------------------------------------
        # Screen capture
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

            light_is_off = False

            last_keepalive = (
                time.monotonic()
            )

            frame_time = (
                1.0 / FPS
            )

            # ------------------------------------------------
            # Main loop
            # ------------------------------------------------

            while True:

                frame_start = (
                    time.monotonic()
                )

                # --------------------------------------------
                # Capture
                # --------------------------------------------

                (
                    left_raw,
                    right_raw,
                ) = capture_screen_colors(
                    sct
                )

                # --------------------------------------------
                # Detect black
                # --------------------------------------------

                max_brightness = max(
                    color_brightness(
                        left_raw
                    ),
                    color_brightness(
                        right_raw
                    ),
                )

                if (
                    max_brightness
                    <= BLACK_THRESHOLD
                ):

                    # ----------------------------------------
                    # IMPORTANT:
                    #
                    # DO NOT send bg_set_rgb(0,0,0).
                    #
                    # That resets set_segment_rgb state.
                    #
                    # Also DO NOT send set_segment_rgb(0,0).
                    # Its behavior is undefined.
                    #
                    # For a true physical dark we have to turn
                    # the background channel off.
                    # ----------------------------------------

                    if not light_is_off:

                        try:

                            tcp.background_power(
                                "off"
                            )

                        except Exception:
                            pass

                        light_is_off = True

                    previous_left = (
                        left_raw
                    )

                    previous_right = (
                        right_raw
                    )

                else:

                    # ----------------------------------------
                    # Screen became non-black.
                    # ----------------------------------------

                    if light_is_off:

                        # Turn the background back on.
                        #
                        # Brightness remains configured at
                        # BG_BRIGHTNESS.
                        try:

                            tcp.background_power(
                                "on"
                            )

                            # Force the first L/R update after
                            # waking the background.
                            tcp.set_segment_rgb(
                                left_raw,
                                right_raw,
                                force=True,
                            )

                        except Exception:
                            pass

                        light_is_off = False

                        previous_left = (
                            left_raw
                        )

                        previous_right = (
                            right_raw
                        )

                    else:

                        # ------------------------------------
                        # Adaptive smoothing
                        # ------------------------------------

                        left = smooth_color(
                            previous_left,
                            left_raw,
                        )

                        right = smooth_color(
                            previous_right,
                            right_raw,
                        )

                        previous_left = left
                        previous_right = right

                        # ------------------------------------
                        # L/R update
                        # ------------------------------------

                        tcp.set_segment_rgb(
                            left,
                            right,
                        )

                # --------------------------------------------
                # UDP keepalive
                # --------------------------------------------

                now = time.monotonic()

                if (
                    now
                    - last_keepalive
                    >= KEEPALIVE_INTERVAL
                ):

                    try:

                        udp.keepalive()

                    except Exception:
                        pass

                    last_keepalive = now

                # --------------------------------------------
                # FPS limiter
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

        # ----------------------------------------------------
        # Only here do we completely turn background OFF.
        # ----------------------------------------------------

        try:

            tcp.background_power(
                "off"
            )

        except Exception:
            pass

    finally:

        tcp.close()
        udp.close()

        print(
            "Done."
        )


if __name__ == "__main__":
    main()
