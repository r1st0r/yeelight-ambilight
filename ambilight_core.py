"""
ambilight_core.py - the Ambilight engine for the Yeelight Monitor Light Bar Pro (lamp15).

Screen analysis, color pipeline, the sender thread that talks to the lamp, and the Engine that ties
them together. Used by the command line (yeelight_ambilight.py) and the tray application (tray_app.py).

How the lamp is driven (measured on lamp15, fw 38):
  * set_segment_rgb carries COLOR ONLY (RGB magnitude is ignored; (0,0,0) turns a side dark);
  * brightness is global and is set once, because the lamp ignores brightness, color temperature and
    power commands for about a second after every color frame;
  * color frames go over the UDP realtime session; power and one-off settings go over TCP (about 60
    commands per minute are allowed) and only after the color stream has been quiet for a moment.
"""

import collections
import colorsys
import dataclasses
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple

from PIL import Image

import yeelight_lan as lan

VERSION = "0.5.0-dev"
BUILD = "r12 zone reach + session end"   # printed at start so you can tell which file is running

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
    lock_s: float = 1.3               # the lamp ignores power/brightness commands for ~1.05 s after a
                                      # segment command; power-off waits this long after the last frame

    center_overlap: float = 0.08      # L/R overlap around the middle
    zone_reach: float = 0.7           # share of each half (from the center outward) that is sampled;
                                      # the outer edges, where game HUDs live, are ignored

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
    black_threshold: float = 7.0      # both sides at or below this: the whole light switches off
    side_dark_threshold: float = 7.0  # a single side at or below this brightness is shown dark ((0,0,0))
    side_wake_threshold: float = 12.0 # ...and lights up again above this (kept at least 4 above the former)
    min_lit_fraction: float = 0.2     # a side with less than this share of lit columns counts as dark
    lit_hysteresis: float = 0.1       # ...and needs this much more to light up again
    wake_threshold: float = 12.0
    dark_hold_s: float = 2.0          # darkness must last this long before power off
    wake_frames: int = 2

    # the bar's front (desk) light is separate from the rear RGB light
    main_light: str = "leave"        # leave | on | off  (applied once at start)
    front_off_on_exit: bool = True   # switch the front (desk) light off too when the program stops

    # diagnostics
    poll_state: bool = False          # --debug: poll the lamp's real state every 5 s


# ============================================================
# Screen analysis
# ============================================================

GRID_COLS = 64
GRID_ROWS = 3


@dataclass
class Zones:
    left: "RGBf"
    right: "RGBf"
    lit_left: float     # share of the zone's columns that are not black, 0..1
    lit_right: float


def analyze_zones(img: Image.Image, overlap: float, lit_threshold: float = 12.0, reach: float = 1.0) -> Zones:
    """Average color and lit share of the left and right zone, using one BOX resize.

    The zones meet at the center of the captured monitor and overlap by `overlap` (a share of the
    width). Each zone extends `reach` (a share of its half) away from the center, so the outer
    edges of the screen, where game HUDs live, are ignored. 64 columns keep the geometry close
    to the settings.
    """
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
    reach_cols = max(1, min(half, round(half * reach)))
    left_cols, right_cols = cols[half - reach_cols: half + ov], cols[half - ov: half + reach_cols]

    def mean(part):
        n = len(part)
        return (sum(p[0] for p in part) / n,
                sum(p[1] for p in part) / n,
                sum(p[2] for p in part) / n)

    def lit(part):
        return sum(1 for p in part if max(p) > lit_threshold) / len(part)

    return Zones(mean(left_cols), mean(right_cols), lit(left_cols), lit(right_cols))


def analyze_strip(img: Image.Image, overlap: float) -> Tuple["RGBf", "RGBf"]:
    """Average colors only (kept for simple callers and tests)."""
    zones = analyze_zones(img, overlap)
    return zones.left, zones.right


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
    t: float = 0.0        # time.monotonic() at the start of the screen grab


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
        self.last_lit = (1.0, 1.0)
        self.last_sides = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        self.out = Output(True, lan.rgb_to_int((255, 255, 255)), lan.rgb_to_int((255, 255, 255)),
                          cfg.bright_max)

    def reset(self) -> None:
        self.__init__(self.cfg)

    def _map_bright(self, luma: float) -> int:
        cfg = self.cfg
        if luma <= cfg.black_threshold:
            return cfg.bright_min
        x = clamp(luma / 255.0, 0.0, 1.0) ** cfg.bright_gamma
        return int(round(cfg.bright_min + (cfg.bright_max - cfg.bright_min) * x))

    def process(self, left: RGBf, right: RGBf, now: float,
                lit: Tuple[float, float] = (1.0, 1.0)) -> Output:
        cfg = self.cfg
        dt = 1 / 60 if self.last_t is None else clamp(now - self.last_t, 0.001, 0.25)
        self.last_t = now

        sides = (left, right)
        side_max = [max(s) for s in sides]
        top = max(side_max)
        raw_luma = cfg.luma_mix * top + (1 - cfg.luma_mix) * (side_max[0] + side_max[1]) / 2
        # A side is dark when it is black, or when only a small part of it is lit. The lamp cannot
        # dim a color, so a thin lit sliver must not light a whole side at full brightness.
        side_wake = max(cfg.side_wake_threshold, cfg.side_dark_threshold + 4.0)
        inst_dark = [side_max[i] <= cfg.side_dark_threshold or lit[i] < cfg.min_lit_fraction
                     for i in (0, 1)]
        # the whole light switches off only when both sides are really black
        dark = all(side_max[i] <= cfg.black_threshold or lit[i] < cfg.min_lit_fraction for i in (0, 1))
        wake_level = cfg.min_lit_fraction + cfg.lit_hysteresis
        self.last_top = top
        self.last_lit = lit
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
            if any(side_max[i] > cfg.wake_threshold and lit[i] >= wake_level for i in (0, 1)):
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
            self.out = Output(False, self.out.left, self.out.right, self.out.bright, now)
            return self.out

        # --- chroma per side ------------------------------------------
        for i in (0, 1):
            # a dark side is shown as (0,0,0), which the lamp renders as no light; the lamp
            # ignores color brightness otherwise, so this is the only way to darken one side
            if self.side_dark[i]:
                if side_max[i] > side_wake and lit[i] >= wake_level:
                    self.side_dark[i] = False
                    self.chroma[i] = None      # snap to the new color
            elif inst_dark[i]:
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
            now,
        )
        return self.out


# ============================================================
# Sender thread: the only place that talks to the lamp
# ============================================================

KEEPALIVE_INTERVAL = 8.0
SESSION_TIMEOUT = 30.0
RECOVER_COOLDOWN = 3.0


class Sender(threading.Thread):
    def __init__(self, cfg: Config, conn: lan.Connection, log: Optional[Callable[[str], None]] = None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.conn = conn
        self.log = log or (lambda message: print(message, file=sys.stderr))
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
        self._lat_sum = 0.0
        self._lat_n = 0
        self._lat_max = 0.0
        self._jobs: Deque[Tuple[str, Callable[[lan.LampTCP], bool], int]] = collections.deque()
        self._job_lock = threading.Lock()
        self._job_next = 0.0
        self.solid: Optional[Tuple[int, int]] = None      # (packed RGB, brightness) = static color mode
        self._solid_done: Optional[Tuple[int, int]] = None
        self._solid_next = 0.0

    def take_latency(self) -> Tuple[float, float]:
        """Average and maximum software latency (ms) of changed frames since the last call."""
        n, total, worst = self._lat_n, self._lat_sum, self._lat_max
        self._lat_sum, self._lat_n, self._lat_max = 0.0, 0, 0.0
        return (total / n * 1000 if n else 0.0, worst * 1000)

    def submit_job(self, label: str, fn: Callable[[lan.LampTCP], bool]) -> None:
        """Queue a one-off lamp command (front light, a slider, ...). While it is pending no color
        frames are sent: the lamp ignores such commands right after a color frame."""
        with self._job_lock:
            self._jobs.append((label, fn, 0))

    def _run_jobs(self, now: float) -> bool:
        """Run queued commands once the stream has been quiet. True while any are pending."""
        with self._job_lock:
            if not self._jobs:
                return False
            label, fn, tries = self._jobs[0]
        if now - self.last_seg_t < self.cfg.lock_s or now < self._job_next:
            return True
        try:
            ok = bool(fn(self.conn.tcp))
        except (OSError, ConnectionError, RuntimeError, ValueError) as exc:
            ok = False
            self.log(f"[sender] {label}: {type(exc).__name__}: {exc}")
        with self._job_lock:
            self._jobs.popleft()
            if not ok and tries < 1:
                self._jobs.appendleft((label, fn, tries + 1))   # one more try a second later
                self._job_next = now + 1.0
        if ok:
            self.log(f"[sender] {label}: done")
            self._force_resend()
        elif tries >= 1:
            self.log(f"[sender] {label}: failed")
        return True

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
                self.log(f"[sender] {type(exc).__name__}: {exc}")
                self._recover()
            delay = period - (time.monotonic() - t0)
            if delay > 0:
                self.stop_event.wait(delay)

    def _force_resend(self) -> None:
        self.last_seg = None
        self.last_bright = None

    def _tick(self, now: float) -> None:
        cfg = self.cfg
        if self._run_jobs(now):
            self._housekeeping(now)
            return
        out = self._latest()
        if out is None:
            return

        if out.power != self.power_actual:
            if out.power:
                if not self.conn.tcp.set_power(True):
                    raise ConnectionError("bg_set_power was not acknowledged")
                self.power_actual = True
                self.off_tries = 0
                self._solid_done = None
                self.log("[sender] bg_set_power on acknowledged")
                # a UDP session opened before a power change may be dropped by the lamp
                self.conn.udp.open()
                self.last_keepalive = now
                self._force_resend()
            elif self._try_power_off(now):
                self.power_actual = False
                self._force_resend()

        if self.solid is None and self._solid_done is not None:     # back to Ambilight: stream again
            self._solid_done = None
            self._force_resend()
        if self.power_actual and out.power and self.solid is not None:
            self._apply_solid(now)
            self._housekeeping(now)
            return

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
                fresh = seg != self.last_seg
                udp.send("set_segment_rgb", [out.left, out.right])
                if fresh and out.t:
                    lat = time.monotonic() - out.t
                    self._lat_sum += lat
                    self._lat_n += 1
                    self._lat_max = max(self._lat_max, lat)
                self.last_seg, self.last_seg_t = seg, now
                self.last_udp = now
                self.frames += 1

        self._housekeeping(now)

    def _apply_solid(self, now: float) -> None:
        """Static color: plain TCP commands (the lamp honours brightness when no frames stream)."""
        target = self.solid
        if target == self._solid_done or now < self._solid_next:
            return
        if now - self.last_seg_t < self.cfg.lock_s:      # wait until the last color frame is forgotten
            return
        color, bright = target
        tcp = self.conn.tcp
        ok = _ok(tcp.call("bg_set_rgb", [color, "sudden", 0], timeout=2.0))
        ok = ok and _set_and_check(tcp, "bg_set_bright", [bright, "sudden", 0], "bg_bright", bright)
        if ok:
            self._solid_done = target
            self._solid_next = now + 0.3
            self.last_seg = None
            self.log(f"[sender] solid color #{color:06x}, brightness {bright}: done")
        else:
            self._solid_next = now + 1.0
            self.log("[sender] solid color was not accepted, trying again")

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
            self.log("[sender] bg_set_power off acknowledged, the lamp confirms it is off")
            return True
        self.off_tries += 1
        self.log(f"[sender] the lamp still reports bg_power={state} after off (try {self.off_tries})")
        if self.off_tries >= 3:
            self.log("[sender] giving up on switching the lamp off")
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
            self.log("[sender] UDP session silent, opening a new one")
            udp.open()
            self._force_resend()

    def _recover(self) -> None:
        now = time.monotonic()
        if now - self.last_recover < RECOVER_COOLDOWN:
            return
        self.last_recover = now
        try:
            if self.conn.rediscover():
                self.log(f"[sender] lamp moved to {self.conn.ip}")
            self.conn.reopen_clients()
        except (OSError, RuntimeError, ValueError) as exc:
            self.log(f"[sender] recovery failed: {exc}")
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
            if self.cfg.front_off_on_exit:
                self._front_off()
        except (OSError, AttributeError):
            pass
        self.conn.close()

    def _front_off(self) -> None:
        """Switch the front (desk) light off. `power` is the combined flag, so it reads off when both are off."""
        for _ in range(3):
            wait = self.cfg.lock_s - (time.monotonic() - self.last_seg_t)
            if wait > 0:
                time.sleep(wait)
            self.conn.tcp.call("set_power", ["off", "sudden", 0], timeout=2.0)
            reply = self.conn.tcp.call("get_prop", ["power"], timeout=1.0)
            result = reply.get("result") if reply else None
            if isinstance(result, list) and result and result[0] == "off":
                self.log("[sender] the front light is off")
                return
            time.sleep(self.cfg.lock_s)
        self.log("[sender] could not confirm that the front light is off")


# ============================================================
# Helpers shared by the command line and the tray application
# ============================================================

CACHE_PATH = os.path.join(os.path.expanduser("~"), ".yeelight_ambilight_lamp.json")


class LampUnavailable(RuntimeError):
    """The lamp could not be found or reached when the engine started."""


def load_config(args) -> Config:
    """Build a Config from an optional JSON file and command-line style overrides."""
    cfg = Config()
    if getattr(args, "config", None):
        with open(args.config, encoding="utf-8") as fh:
            data = json.load(fh)
        names = {f.name for f in dataclasses.fields(Config)}
        unknown = set(data) - names
        if unknown:
            sys.exit(f"Unknown config keys: {', '.join(sorted(unknown))}")
        cfg = dataclasses.replace(cfg, **data)
    overrides = {
        "ip": getattr(args, "ip", None), "monitor": getattr(args, "monitor", None),
        "send_fps": getattr(args, "send_fps", None), "capture_fps": getattr(args, "capture_fps", None),
        "bright_max": getattr(args, "bright_max", None), "main_light": getattr(args, "main_light", None),
    }
    cfg = dataclasses.replace(cfg, **{k: v for k, v in overrides.items() if v is not None})
    if cfg.main_light not in ("leave", "on", "off"):
        sys.exit("main_light must be leave, on or off")
    return cfg


def _ok(reply: Optional[dict]) -> bool:
    return bool(reply) and reply.get("result") == ["ok"]


def apply_main_light(conn: lan.Connection, mode: str) -> Optional[bool]:
    """Switch the front (desk) light once. Returns None when nothing was requested."""
    if mode not in ("on", "off"):
        return None
    try:
        reply = conn.tcp.call("set_power", [mode, "sudden", 0], timeout=2.0)
    except OSError:
        return False
    return _ok(reply)


def _set_and_check(tcp: lan.LampTCP, method: str, params: list, prop: str, expected) -> bool:
    """Send a command and confirm with the lamp that the property really changed."""
    if not _ok(tcp.call(method, params, timeout=2.0)):
        return False
    check = tcp.call("get_prop", [prop], timeout=1.0)
    result = check.get("result") if check else None
    return bool(result) and str(result[0]) == str(expected)


# ============================================================
# Screen grabber
# ============================================================

class MssGrabber:
    """Captures the top strip of one monitor with mss. Use as a context manager."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._sct = None
        self._region: dict = {}
        self.width = self.height = self.strip = 0

    def __enter__(self) -> "MssGrabber":
        import mss  # imported here so the logic stays testable without a display
        mss_class = getattr(mss, "MSS", None) or mss.mss
        self._sct = mss_class()
        monitors = self._sct.monitors
        index = self.cfg.monitor if 0 < self.cfg.monitor < len(monitors) else 1
        mon = monitors[index]
        self._region = {
            "left": mon["left"], "top": mon["top"], "width": mon["width"],
            "height": max(1, int(mon["height"] * self.cfg.top_percent)),
        }
        self.width, self.height, self.strip = mon["width"], mon["height"], self._region["height"]
        return self

    def __exit__(self, *exc_info) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None

    def grab(self) -> Image.Image:
        shot = self._sct.grab(self._region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# ============================================================
# Engine
# ============================================================

class Engine:
    """The whole Ambilight as one controllable object.

    run() blocks (used by the command line); start() runs it in a background thread (used by the
    tray application). Controls are thread safe: one-off lamp commands are queued to the sender,
    which pauses the color stream while the lamp accepts them.
    """

    def __init__(self, cfg: Config, log: Optional[Callable[[str], None]] = None,
                 grabber_factory: Optional[Callable[[Config], object]] = None):
        self.cfg = cfg
        self._log_cb = log or print
        self._grabber_factory = grabber_factory or MssGrabber
        self.logs: Deque[Tuple[float, str]] = collections.deque(maxlen=2000)
        self.status = "stopped"
        self.status_cb: Optional[Callable[[str], None]] = None
        self.debug = False
        self.save_strip: Optional[str] = None
        self.hint = ""                              # an extra line printed after start-up (CLI)
        self.sender: Optional[Sender] = None
        self.stats: Dict[str, float] = {"capture_fps": 0.0, "lamp_fps": 0.0, "sw_avg": 0.0, "sw_max": 0.0}
        self._ambilight_on = True
        self.rear_mode = "ambilight"                # or "solid"
        self.solid_color = 0x4F6BFF
        self.solid_bright = 80
        self.last_colors = (0, 0)                    # (left, right) packed RGB as sent to the lamp
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----- logging and status ------------------------------------------------
    def log(self, message: str) -> None:
        self.logs.append((time.time(), message))
        try:
            self._log_cb(message)
        except Exception:
            pass

    def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            if self.status_cb:
                try:
                    self.status_cb(status)
                except Exception:
                    pass

    @property
    def ambilight_on(self) -> bool:
        return self._ambilight_on

    def snapshot(self) -> dict:
        sender = self.sender
        data = dict(self.stats)
        data.update(status=self.status, ambilight_on=self._ambilight_on,
                    errors=sender.errors if sender else 0,
                    lamp_state=sender.lamp_state if sender else "-")
        return data

    # ----- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_thread, name="ambilight-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def _run_thread(self) -> None:
        try:
            self.run(retry=True)
        except Exception as exc:
            import traceback
            self.log("[engine] stopped by an error: " + "".join(
                traceback.format_exception_only(type(exc), exc)).strip())
        finally:
            self._set_status("stopped")

    def run(self, retry: bool = True) -> None:
        """Blocking main loop. With retry=False a lamp that cannot be reached raises LampUnavailable."""
        while not self._stop.is_set():
            self._set_status("connecting")
            conn = lan.Connection(ip=self.cfg.ip, cache_path=CACHE_PATH)
            try:
                conn.open()
            except (OSError, RuntimeError, ValueError) as exc:
                self._set_status("lamp not found")
                if not retry:
                    raise LampUnavailable(str(exc)) from exc
                self.log(f"Cannot reach the lamp: {exc}")
                self._stop.wait(3.0)
                continue
            try:
                self._serve(conn)
            except Exception as exc:
                if not retry:
                    raise
                self._set_status("error")
                self.log(f"[engine] {type(exc).__name__}: {exc}")
                self._stop.wait(3.0)
            finally:
                conn.close()

    # ----- the serving loop --------------------------------------------------
    def _serve(self, conn: lan.Connection) -> None:
        cfg = self.cfg
        info = conn.info
        self.log(f"Lamp: {info.get('model', '?')} fw {info.get('fw_ver', '?')} "
                 f"{conn.ip} id {conn.device_id or 'pinned by --ip'}")
        brightness = (f"{cfg.bright_min}-{cfg.bright_max} (adaptive, experimental)" if cfg.adaptive_brightness
                      else f"{cfg.bright_max} (fixed)")
        self.log(f"Capture {cfg.capture_fps} fps, lamp {cfg.send_fps} fps, brightness {brightness}")
        main_ok = apply_main_light(conn, cfg.main_light)
        if main_ok is not None:
            self.log(f"Front (desk) light -> {cfg.main_light}: {'ok' if main_ok else 'FAILED'}")
        if self.hint:
            self.log(self.hint)

        sender = Sender(cfg, conn, log=self.log)
        pipeline = Pipeline(cfg)
        self.sender = sender
        sender.start()
        self._set_status("running")
        try:
            self._capture_loop(sender, pipeline)
        except KeyboardInterrupt:
            self.log("\nStopping...")
            self._stop.set()
        finally:
            self.log("Switching the lamp off...")
            sender.shutdown()
            self.sender = None

    def _capture_loop(self, sender: Sender, pipeline: Pipeline) -> None:
        frame_time = 1.0 / self.cfg.capture_fps
        while not self._stop.is_set():
            try:
                with self._grabber_factory(self.cfg) as grab:
                    self.log(f"Screen {grab.width}x{grab.height}, strip {grab.strip} px\n")
                    self._grab_loop(grab, sender, pipeline, frame_time)
            except Exception as exc:     # the display changed, the session is locked, ...
                self.log(f"[capture] {type(exc).__name__}: {exc}")
                self._stop.wait(2.0)

    def _grab_loop(self, grab, sender: Sender, pipeline: Pipeline, frame_time: float) -> None:
        cfg = self.cfg
        stat_t = time.monotonic()
        stat_frames = 0
        stat_cost = 0.0
        last_save = 0.0
        fail_streak = 0
        active = True
        while not self._stop.is_set():
            t0 = time.monotonic()
            sender.solid = ((self.solid_color, self.solid_bright)
                            if self.rear_mode == "solid" and self._ambilight_on else None)
            if self.rear_mode == "solid" and self._ambilight_on:     # static color: no capturing
                active = False
                sender.submit(Output(True, 0, 0, self.solid_bright, t0))
                self.last_colors = (self.solid_color, self.solid_color)
                self._stop.wait(0.1)
                continue
            if not self._ambilight_on:            # paused from the outside: the rear light goes off
                active = False
                sender.submit(Output(False, 0, 0, cfg.bright_max, t0))
                self._stop.wait(0.1)
                continue
            if not active:
                pipeline.reset()
                active = True
            try:
                img = grab.grab()
                fail_streak = 0
            except Exception as exc:
                fail_streak += 1
                if fail_streak == 1:
                    self.log(f"[capture] {type(exc).__name__}: {exc}")
                if fail_streak >= 10:
                    raise                          # the outer loop recreates the grabber
                self._stop.wait(0.3)
                continue
            if self.save_strip and t0 - last_save >= 2.0:
                last_save = t0
                try:
                    img.save(self.save_strip)
                except OSError as exc:
                    self.log(f"[save-strip] cannot write {self.save_strip}: {exc}. "
                             "Use a writable path, e.g. %TEMP%\\strip.png")
                    self.save_strip = None
            zones = analyze_zones(img, cfg.center_overlap, max(cfg.wake_threshold, cfg.side_dark_threshold),
                                  cfg.zone_reach)
            out = pipeline.process(zones.left, zones.right, t0, (zones.lit_left, zones.lit_right))
            sender.submit(out)
            self.last_colors = (out.left, out.right) if out.power else (0, 0)

            cost = time.monotonic() - t0
            stat_frames += 1
            stat_cost += cost
            if t0 - stat_t >= 2.0:
                span = t0 - stat_t
                sw_avg, sw_max = sender.take_latency()
                lamp_fps = sender.frames / span
                sender.frames = 0
                self.stats = {"capture_fps": stat_frames / span, "lamp_fps": lamp_fps,
                              "sw_avg": sw_avg, "sw_max": sw_max}
                if self.debug:
                    self.log(f"capture {stat_frames / span:5.1f} fps ({stat_cost / stat_frames * 1000:4.1f} ms/frame)"
                             f" | lamp {lamp_fps:5.1f}/s | sw {sw_avg:3.0f}/{sw_max:3.0f} ms"
                             f" | bright {out.bright:3d}"
                             f" | power {'on ' if out.power else 'off'} | errors {sender.errors}"
                             f" | {pipeline.state} top={pipeline.last_top:5.1f}"
                             f" | lamp power/bg_power/bg_bright/bg_rgb={sender.lamp_state}"
                             f" lit={pipeline.last_lit[0] * 100:.0f}%/{pipeline.last_lit[1] * 100:.0f}%"
                             f" L={tuple(int(v) for v in pipeline.last_sides[0])}"
                             f" R={tuple(int(v) for v in pipeline.last_sides[1])}")
                stat_t, stat_frames, stat_cost = t0, 0, 0.0

            delay = frame_time - cost
            if delay > 0:
                time.sleep(delay)

    # ----- controls (thread safe) --------------------------------------------
    def set_ambilight(self, on: bool) -> None:
        """Ambilight on: the rear light follows the screen. Off: the rear light is switched off."""
        self._ambilight_on = bool(on)
        self.log(f"Ambilight {'on' if on else 'off'}")

    def set_rear_mode(self, mode: str) -> None:
        """"ambilight" follows the screen; "solid" shows one static color."""
        self.rear_mode = "solid" if mode == "solid" else "ambilight"
        self.log(f"Rear light mode: {self.rear_mode}")

    def set_solid_color(self, rgb: int) -> None:
        self.solid_color = int(rgb) & 0xFFFFFF
        self.log(f"Solid color #{self.solid_color:06x}")

    def set_solid_brightness(self, value: int) -> None:
        self.solid_bright = int(clamp(value, 1, 100))
        self.log(f"Solid color brightness {self.solid_bright}")

    def _job(self, label: str, fn: Callable[[lan.LampTCP], bool]) -> bool:
        sender = self.sender
        if sender is None:
            self.log(f"{label}: the lamp is not connected")
            return False
        sender.submit_job(label, fn)
        return True

    def set_front_power(self, on: bool) -> bool:
        state = "on" if on else "off"
        return self._job(f"front light {state}",
                         lambda tcp: _ok(tcp.call("set_power", [state, "sudden", 0], timeout=2.0)))

    def set_front_brightness(self, value: int) -> bool:
        value = int(clamp(value, 1, 100))
        return self._job(f"front brightness {value}",
                         lambda tcp: _set_and_check(tcp, "set_bright", [value, "sudden", 0], "bright", value))

    def set_front_ct(self, kelvin: int) -> bool:
        kelvin = int(clamp(kelvin, 2700, 6500))
        return self._job(f"front color temperature {kelvin} K",
                         lambda tcp: _set_and_check(tcp, "set_ct_abx", [kelvin, "sudden", 0], "ct", kelvin))

    def set_rear_brightness(self, value: int) -> bool:
        value = int(clamp(value, 1, 100))
        self.cfg.bright_max = value                 # also used when the light wakes up again
        return self._job(f"rear brightness {value}",
                         lambda tcp: _set_and_check(tcp, "bg_set_bright", [value, "sudden", 0], "bg_bright", value))
