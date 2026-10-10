#!/usr/bin/env python3
"""
Yeelight Ambilight v0.4 - screen-top sync for the Yeelight Monitor Light Bar Pro (lamp15).

How the lamp is driven (measured on lamp15, fw 38):
  * set_segment_rgb carries COLOR ONLY (RGB magnitude is ignored), so each side
    gets a fully-bright, normalised color ("chroma");
  * brightness is global (bg_set_bright) and follows the scene brightness ("luma");
  * both go over the UDP realtime session; power on/off goes over TCP and only
    after a sustained dark scene, because the lamp needs about a second to switch.
"""

import argparse
import colorsys
import dataclasses
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image

import yeelight_lan as lan

BUILD = "r7 lock-aware power-off"   # printed at start so you can tell which file is running

RGBf = Tuple[float, float, float]


# ============================================================
# Configuration (override with --config config.json or CLI flags)
# ============================================================

@dataclass
class Config:
    ip: Optional[str] = None          # None = auto-discovery
    monitor: int = 1                  # mss monitor index (1 = primary)
    top_percent: float = 0.15
    capture_fps: int = 60
    send_fps: int = 30                # lamp frames per second (UDP)
    bright_fps: int = 15              # max bg_set_bright updates per second
    refresh_s: float = 0.5            # resend unchanged state (packet-loss repair)
    udp_gap_s: float = 0.02           # minimum time between two UDP datagrams
    lock_s: float = 1.6               # the lamp ignores power/brightness commands for ~1 s after a
                                      # segment command; power-off waits this long after the last frame

    center_overlap: float = 0.08      # L/R overlap around the middle

    # brightness (global, 1..100)
    adaptive_brightness: bool = False  # EXPERIMENTAL: the lamp ignores bg_set_bright for ~1 s after every
                                       # segment command, so it cannot follow the scene while streaming.
                                       # False = fixed brightness (bright_max), set at start and on wake
    bright_min: int = 1
    bright_max: int = 60
    bright_gamma: float = 0.9
    bright_deadband: int = 2
    luma_mix: float = 0.5             # 0 = mean of sides, 1 = brighter side

    # color
    sat_boost: float = 1.1
    chroma_min: float = 10.0          # below this a side is too dark to trust its color

    # smoothing (time constants in seconds)
    tau_slow: float = 0.15
    tau_fast: float = 0.02
    cut_distance: float = 60.0        # chroma change that counts as a fast scene
    luma_tau_up: float = 0.04
    luma_tau_down: float = 0.25
    luma_cut: float = 70.0            # luma jump that bypasses smoothing

    # dark scenes
    black_threshold: float = 7.0
    wake_threshold: float = 12.0
    dark_hold_s: float = 2.0          # darkness must last this long before power off
    wake_frames: int = 2

    # the bar's front (desk) light is separate from the rear RGB light
    main_light: str = "leave"        # leave | on | off  (applied once at start)

    # diagnostics
    poll_state: bool = False          # --debug: poll the lamp's real state every 5 s


# ============================================================
# Screen analysis
# ============================================================

GRID_COLS = 16
GRID_ROWS = 3


def analyze_strip(img: Image.Image, overlap: float) -> Tuple[RGBf, RGBf]:
    """Average color of the left and right part of a strip, using one BOX resize."""
    small = img.resize((GRID_COLS, GRID_ROWS), Image.Resampling.BOX)
    data = small.tobytes()

    cols = []
    for c in range(GRID_COLS):
        r = g = b = 0
        for row in range(GRID_ROWS):
            i = (row * GRID_COLS + c) * 3
            r += data[i]
            g += data[i + 1]
            b += data[i + 2]
        cols.append((r / GRID_ROWS, g / GRID_ROWS, b / GRID_ROWS))

    half = GRID_COLS // 2
    ov = max(0, round(GRID_COLS * overlap / 2))

    def mean(part):
        n = len(part)
        return (sum(p[0] for p in part) / n,
                sum(p[1] for p in part) / n,
                sum(p[2] for p in part) / n)

    return mean(cols[: half + ov]), mean(cols[half - ov:])


# ============================================================
# Color / brightness pipeline (pure logic, no I/O)
# ============================================================

def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def ema(prev: float, target: float, dt: float, tau: float) -> float:
    if tau <= 0:
        return target
    return prev + (target - prev) * (1.0 - math.exp(-dt / tau))


def distance(a: RGBf, b: RGBf) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def normalize_chroma(rgb: RGBf, sat_boost: float) -> RGBf:
    """Full-value color with the same hue; saturation slightly boosted."""
    h, s, _ = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * sat_boost), 1.0)
    return (r * 255, g * 255, b * 255)


@dataclass
class Output:
    power: bool
    left: int
    right: int
    bright: int


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.chroma = [None, None]
        self.luma: Optional[float] = None
        self.last_t: Optional[float] = None
        self.state = "active"          # active | off
        self.dark_since: Optional[float] = None
        self.wake_count = 0
        self.side_dark = [False, False]
        self.bright = cfg.bright_max
        self.last_top = 0.0
        self.last_sides = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        self.out = Output(True, lan.rgb_to_int((255, 255, 255)), lan.rgb_to_int((255, 255, 255)),
                          cfg.bright_max)

    def _map_bright(self, luma: float) -> int:
        cfg = self.cfg
        if luma <= cfg.black_threshold:
            return cfg.bright_min
        x = clamp(luma / 255.0, 0.0, 1.0) ** cfg.bright_gamma
        return int(round(cfg.bright_min + (cfg.bright_max - cfg.bright_min) * x))

    def process(self, left: RGBf, right: RGBf, now: float) -> Output:
        cfg = self.cfg
        dt = 1 / 60 if self.last_t is None else clamp(now - self.last_t, 0.001, 0.25)
        self.last_t = now

        sides = (left, right)
        side_max = [max(s) for s in sides]
        top = max(side_max)
        raw_luma = cfg.luma_mix * top + (1 - cfg.luma_mix) * (side_max[0] + side_max[1]) / 2
        dark = top <= cfg.black_threshold
        self.last_top = top
        self.last_sides = sides

        # --- power state with hysteresis ------------------------------
        if self.state == "active":
            if dark:
                if self.dark_since is None:
                    self.dark_since = now
                elif now - self.dark_since >= cfg.dark_hold_s:
                    self.state = "off"
                    self.wake_count = 0
            else:
                self.dark_since = None
        else:
            if top > cfg.wake_threshold:
                self.wake_count += 1
                if self.wake_count >= cfg.wake_frames:
                    self.state = "active"
                    self.dark_since = None
                    self.luma = None          # snap to the new scene
                    self.chroma = [None, None]
                    self.side_dark = [True, True]
            else:
                self.wake_count = 0

        if self.state == "off":
            self.out = Output(False, self.out.left, self.out.right, self.out.bright)
            return self.out

        # --- chroma per side ------------------------------------------
        for i in (0, 1):
            # a dark side is shown as (0,0,0), which the lamp renders as no light; the lamp
            # ignores color brightness otherwise, so this is the only way to darken one side
            if self.side_dark[i]:
                if side_max[i] > cfg.wake_threshold:
                    self.side_dark[i] = False
                    self.chroma[i] = None      # snap to the new color
            elif side_max[i] <= cfg.black_threshold:
                self.side_dark[i] = True
            if self.side_dark[i]:
                continue
            if side_max[i] < cfg.chroma_min:
                continue                       # too dark to trust its color: hold the last one
            target = normalize_chroma(sides[i], cfg.sat_boost)
            prev = self.chroma[i]
            if prev is None:
                self.chroma[i] = target
            else:
                ratio = min(1.0, distance(prev, target) / cfg.cut_distance)
                tau = cfg.tau_slow + (cfg.tau_fast - cfg.tau_slow) * ratio
                self.chroma[i] = tuple(ema(p, t, dt, tau) for p, t in zip(prev, target))

        white = (255.0, 255.0, 255.0)
        c_left = self.chroma[0] or white
        c_right = self.chroma[1] or white

        # --- global brightness ----------------------------------------
        target_luma = 0.0 if dark else raw_luma
        if self.luma is None or abs(target_luma - self.luma) >= cfg.luma_cut:
            self.luma = target_luma
        else:
            tau = cfg.luma_tau_up if target_luma > self.luma else cfg.luma_tau_down
            self.luma = ema(self.luma, target_luma, dt, tau)

        if cfg.adaptive_brightness:
            new_bright = self._map_bright(self.luma)
            at_edge = new_bright in (cfg.bright_min, cfg.bright_max)
            if at_edge or abs(new_bright - self.bright) >= cfg.bright_deadband:
                self.bright = new_bright
        else:
            self.bright = cfg.bright_max

        self.out = Output(
            True,
            0 if self.side_dark[0] else lan.rgb_to_int(tuple(int(round(v)) for v in c_left)),
            0 if self.side_dark[1] else lan.rgb_to_int(tuple(int(round(v)) for v in c_right)),
            self.bright,
        )
        return self.out


# ============================================================
# Sender thread: the only place that talks to the lamp
# ============================================================

KEEPALIVE_INTERVAL = 8.0
SESSION_TIMEOUT = 30.0
RECOVER_COOLDOWN = 3.0


class Sender(threading.Thread):
    def __init__(self, cfg: Config, conn: lan.Connection):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.conn = conn
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._out: Optional[Output] = None

        self.power_actual: Optional[bool] = None
        self.last_seg: Optional[Tuple[int, int]] = None
        self.last_seg_t = 0.0
        self.last_bright: Optional[int] = None
        self.last_bright_t = 0.0
        self.last_keepalive = 0.0
        self.last_recover = 0.0
        self.last_udp = 0.0
        self.frames = 0
        self.errors = 0
        self.lamp_state = "-"
        self.last_poll = 0.0
        self.last_off_try = 0.0
        self.off_tries = 0

    def submit(self, out: Output) -> None:
        with self._lock:
            self._out = out

    def _latest(self) -> Optional[Output]:
        with self._lock:
            return self._out

    def run(self) -> None:
        period = 1.0 / self.cfg.send_fps
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            try:
                self._tick(t0)
            except (OSError, ConnectionError, RuntimeError, ValueError) as exc:
                self.errors += 1
                print(f"[sender] {type(exc).__name__}: {exc}", file=sys.stderr)
                self._recover()
            delay = period - (time.monotonic() - t0)
            if delay > 0:
                self.stop_event.wait(delay)

    def _force_resend(self) -> None:
        self.last_seg = None
        self.last_bright = None

    def _tick(self, now: float) -> None:
        cfg = self.cfg
        out = self._latest()
        if out is None:
            return

        if out.power != self.power_actual:
            if out.power:
                if not self.conn.tcp.set_power(True):
                    raise ConnectionError("bg_set_power was not acknowledged")
                self.power_actual = True
                self.off_tries = 0
                print("[sender] bg_set_power on acknowledged")
                # a UDP session opened before a power change may be dropped by the lamp
                self.conn.udp.open()
                self.last_keepalive = now
                self._force_resend()
            elif self._try_power_off(now):
                self.power_actual = False
                self._force_resend()

        if self.power_actual and out.power and now - self.last_udp >= cfg.udp_gap_s:
            udp = self.conn.udp
            seg = (out.left, out.right)
            black = out.left == 0 and out.right == 0
            seg_due = seg != self.last_seg or (not black and now - self.last_seg_t >= cfg.refresh_s)
            bright_due = (now - self.last_bright_t >= 1.0 / cfg.bright_fps
                          and (out.bright != self.last_bright
                               or (cfg.adaptive_brightness
                                   and now - self.last_bright_t >= cfg.refresh_s)))
            # one datagram per tick: the lamp may drop datagrams that arrive back to back
            if bright_due:
                udp.send("bg_set_bright", [out.bright, "sudden", 0])
                self.last_bright, self.last_bright_t = out.bright, now
                self.last_udp = now
            elif seg_due:
                udp.send("set_segment_rgb", [out.left, out.right])
                self.last_seg, self.last_seg_t = seg, now
                self.last_udp = now
                self.frames += 1

        self._housekeeping(now)

    def _try_power_off(self, now: float) -> bool:
        """Switch the rear light off, the way the lamp allows it.

        The lamp acknowledges a power command but ignores it for about a second after a
        segment command. So wait until the stream has been quiet for lock_s, send the
        command, then ask the lamp whether it really switched off.
        """
        cfg = self.cfg
        if now - self.last_seg_t < cfg.lock_s or now - self.last_off_try < cfg.lock_s:
            return False
        self.last_off_try = now
        if not self.conn.tcp.set_power(False):
            raise ConnectionError("bg_set_power was not acknowledged")
        return self._verify_off()

    def _verify_off(self) -> bool:
        reply = self.conn.tcp.call("get_prop", ["bg_power"], timeout=1.0)
        result = reply.get("result") if reply else None
        state = result[0] if isinstance(result, list) and result else None
        if state == "off":
            self.off_tries = 0
            print("[sender] bg_set_power off acknowledged, the lamp confirms it is off")
            return True
        self.off_tries += 1
        print(f"[sender] the lamp still reports bg_power={state} after off (try {self.off_tries})",
              file=sys.stderr)
        if self.off_tries >= 3:
            print("[sender] giving up on switching the lamp off", file=sys.stderr)
            self.off_tries = 0
            return True
        return False

    def _housekeeping(self, now: float) -> None:
        udp = self.conn.udp
        if self.cfg.poll_state and now - self.last_poll >= 5.0:
            self.last_poll = now
            reply = self.conn.tcp.call("get_prop", ["power", "bg_power", "bg_bright", "bg_rgb"],
                                       timeout=0.5)
            if reply and isinstance(reply.get("result"), list):
                self.lamp_state = "/".join(str(v) for v in reply["result"])
            else:
                self.lamp_state = "no reply"
        if now - self.last_keepalive >= KEEPALIVE_INTERVAL:
            udp.keepalive()
            self.last_keepalive = now
        udp.drain()
        if now - udp.last_alive > SESSION_TIMEOUT:
            print("[sender] UDP session silent, opening a new one", file=sys.stderr)
            udp.open()
            self._force_resend()

    def _recover(self) -> None:
        now = time.monotonic()
        if now - self.last_recover < RECOVER_COOLDOWN:
            return
        self.last_recover = now
        try:
            if self.conn.rediscover():
                print(f"[sender] lamp moved to {self.conn.ip}", file=sys.stderr)
            self.conn.reopen_clients()
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"[sender] recovery failed: {exc}", file=sys.stderr)
            return
        self.power_actual = None
        self._force_resend()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.join(timeout=2.0)
        try:
            if self.power_actual is not False:
                for _ in range(3):
                    wait = self.cfg.lock_s - (time.monotonic() - self.last_seg_t)
                    if wait > 0:
                        time.sleep(wait)       # the lamp ignores power commands right after a frame
                    if self.conn.tcp.set_power(False) and self._verify_off():
                        break
                    time.sleep(self.cfg.lock_s)
        except (OSError, AttributeError):
            pass
        self.conn.close()


# ============================================================
# Main
# ============================================================

def load_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            data = json.load(fh)
        names = {f.name for f in dataclasses.fields(Config)}
        unknown = set(data) - names
        if unknown:
            sys.exit(f"Unknown config keys: {', '.join(sorted(unknown))}")
        cfg = dataclasses.replace(cfg, **data)
    overrides = {
        "ip": args.ip, "monitor": args.monitor, "send_fps": args.send_fps,
        "capture_fps": args.capture_fps, "bright_max": args.bright_max,
        "main_light": args.main_light,
    }
    cfg = dataclasses.replace(cfg, **{k: v for k, v in overrides.items() if v is not None})
    if cfg.main_light not in ("leave", "on", "off"):
        sys.exit("main_light must be leave, on or off")
    return cfg


def apply_main_light(conn: lan.Connection, mode: str) -> Optional[bool]:
    """Switch the front (desk) light once. Returns None when nothing was requested."""
    if mode not in ("on", "off"):
        return None
    try:
        reply = conn.tcp.call("set_power", [mode, "sudden", 0], timeout=2.0)
    except OSError:
        return False
    return bool(reply) and reply.get("result") == ["ok"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Yeelight Ambilight v0.4")
    parser.add_argument("--ip", help="lamp IP (default: auto-discovery)")
    parser.add_argument("--config", help="JSON file with Config overrides")
    parser.add_argument("--monitor", type=int)
    parser.add_argument("--send-fps", type=int)
    parser.add_argument("--capture-fps", type=int)
    parser.add_argument("--bright-max", type=int)
    parser.add_argument("--main-light", choices=["leave", "on", "off"],
                        help="front (desk) light of the bar: leave (default), on or off; not restored on exit")
    parser.add_argument("--discover", action="store_true", help="list lamps and exit")
    parser.add_argument("--debug", action="store_true", help="print stats every 2 s")
    parser.add_argument("--trace", action="store_true", help="log every command sent to the lamp and every reply (verbose)")
    parser.add_argument("--save-strip", metavar="PNG",
                        help="save the captured strip every 2 s (overwritten) to see what the script sees")
    args = parser.parse_args()

    if args.discover:
        for dev_id, d in lan.discover().items():
            print(f"{dev_id}  {d['model']}  fw {d['fw_ver']}  {d['ip']}:{d['port']}  power={d['power']}")
        return

    cfg = load_config(args)
    cfg.poll_state = args.debug
    lan.TRACE = args.trace
    cache_path = os.path.join(os.path.expanduser("~"), ".yeelight_ambilight_lamp.json")
    conn = lan.Connection(ip=cfg.ip, cache_path=cache_path)

    print("=" * 55)
    print(f"Yeelight Ambilight v0.4 ({BUILD})")
    print("=" * 55)
    try:
        conn.open()
    except (OSError, RuntimeError, ValueError) as exc:
        sys.exit(f"Cannot reach the lamp: {exc}")
    info = conn.info
    print(f"Lamp: {info.get('model', '?')} fw {info.get('fw_ver', '?')} "
          f"{conn.ip} id {conn.device_id or 'pinned by --ip'}")
    brightness = (f"{cfg.bright_min}-{cfg.bright_max} (adaptive, experimental)" if cfg.adaptive_brightness
                  else f"{cfg.bright_max} (fixed)")
    print(f"Capture {cfg.capture_fps} fps, lamp {cfg.send_fps} fps, brightness {brightness}")
    main_ok = apply_main_light(conn, cfg.main_light)
    if main_ok is not None:
        print(f"Front (desk) light -> {cfg.main_light}: {'ok' if main_ok else 'FAILED'}")
    print("Press Ctrl+C to stop.\n")

    import mss  # imported here so the logic above stays testable without a display
    mss_class = getattr(mss, "MSS", None) or mss.mss

    sender = Sender(cfg, conn)
    pipeline = Pipeline(cfg)
    sender.start()

    try:
        with mss_class() as sct:
            mon = sct.monitors[cfg.monitor]
            region = {
                "left": mon["left"], "top": mon["top"], "width": mon["width"],
                "height": max(1, int(mon["height"] * cfg.top_percent)),
            }
            print(f"Screen {mon['width']}x{mon['height']}, strip {region['height']} px\n")

            frame_time = 1.0 / cfg.capture_fps
            stat_t = time.monotonic()
            stat_frames = 0
            stat_cost = 0.0
            last_save = 0.0

            while True:
                t0 = time.monotonic()
                shot = sct.grab(region)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                if args.save_strip and t0 - last_save >= 2.0:
                    last_save = t0
                    try:
                        img.save(args.save_strip)
                    except OSError as exc:
                        print(f"[save-strip] cannot write {args.save_strip}: {exc}. "
                              "Use a writable path, e.g. %TEMP%\\strip.png", file=sys.stderr)
                        args.save_strip = None
                left, right = analyze_strip(img, cfg.center_overlap)
                out = pipeline.process(left, right, t0)
                sender.submit(out)

                cost = time.monotonic() - t0
                stat_frames += 1
                stat_cost += cost
                if args.debug and t0 - stat_t >= 2.0:
                    span = t0 - stat_t
                    print(f"capture {stat_frames / span:5.1f} fps ({stat_cost / stat_frames * 1000:4.1f} ms/frame)"
                          f" | lamp {sender.frames / span:5.1f}/s | bright {out.bright:3d}"
                          f" | power {'on ' if out.power else 'off'} | errors {sender.errors}"
                          f" | {pipeline.state} top={pipeline.last_top:5.1f}"
                          f" | lamp power/bg_power/bg_bright/bg_rgb={sender.lamp_state}"
                          f" L={tuple(int(v) for v in pipeline.last_sides[0])}"
                          f" R={tuple(int(v) for v in pipeline.last_sides[1])}")
                    stat_t, stat_frames, stat_cost = t0, 0, 0.0
                    sender.frames = 0

                delay = frame_time - cost
                if delay > 0:
                    time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        print("Switching the lamp off...")
        sender.shutdown()
        print("Done.")


if __name__ == "__main__":
    main()
