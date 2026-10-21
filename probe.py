#!/usr/bin/env python3
"""
probe.py - diagnostic tool for the Yeelight Monitor Light Bar Pro (lamp15).

Standard library only. It does NOT touch your main script. Use it to answer
the open questions of the project with measurements instead of guesses:

  python probe.py discover              # SSDP discovery on every local interface
  python probe.py info                  # get_prop + what the lamp says it supports
  python probe.py rtt                   # TCP round-trip time of set_segment_rgb
  python probe.py segments              # L/R behaviour: cold vs warm, swaps
  python probe.py gray                  # is RGB magnitude ignored? (gray -> white?)
  python probe.py udp                   # can set_segment_rgb / bg_* go over UDP?
  python probe.py udpfps --fps 30       # sustained UDP rate, you watch the lamp
  python probe.py wake                  # software-side latency of power off/on
  python probe.py chroma                # what does udp_chroma_sess_new return?
  python probe.py dark                  # residual glow vs global brightness
  python probe.py poweroff              # why does light stay on after bg_set_power off?
  python probe.py maintest              # does the rear light obey after the front light is off?
  python probe.py udpgap                # does the lamp drop UDP datagrams that arrive back to back?
  python probe.py brightstream         # does a brightness change survive when the UDP stream resumes?
  python probe.py segbright             # which commands change brightness in segment mode?
  python probe.py segmix                # brightness routes after segments were sent over UDP
  python probe.py tcpsustain --fps 30   # sustained TCP segment stream, watch for quota errors
  python probe.py lock                  # how long is bg_set_bright ignored after a UDP segment?
  python probe.py frontlock             # is the FRONT light's brightness ignored after a UDP segment?
  python probe.py frontct               # front light color temperature: accepted range, lock after segments
  python probe.py dither                # can alternating color / (0,0,0) frames emulate dimmer light?
  python probe.py udpmag                # do UDP segment colors honour brightness (RGB magnitude)?
  python probe.py raw set_power off sudden 0   # any single command + power state

Pass --ip 192.168.x.y to skip discovery. Everything is logged to stdout,
paste the output back into the chat.
"""

import argparse
import json
import re
import socket
import statistics
import sys
import time

TCP_PORT = 55443
UDP_PORT = 55444
SSDP_ADDR = ("239.255.255.250", 1982)
SSDP_MSG = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1982\r\n"
    b'MAN: "ssdp:discover"\r\n'
    b"ST: wifi_bulb\r\n\r\n"
)

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
MAGENTA = (255, 0, 255)


def rgb_int(rgb):
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]


def dumps(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\r\n").encode()


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def local_ips():
    ips = set()
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(addrs)
    except OSError:
        pass
    try:
        # Address of the interface that has the default route.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # no packet is sent for UDP connect
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    ips = {ip for ip in ips if not ip.startswith("127.")}
    return sorted(ips) or ["0.0.0.0"]


def discover(timeout=2.0):
    """Return {device_id: info}. Searches from every local interface."""
    found = {}
    for local in local_ips():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.bind((local, 0))
            if local != "0.0.0.0":
                s.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(local),
                )
            s.sendto(SSDP_MSG, SSDP_ADDR)
            end = time.monotonic() + timeout
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                s.settimeout(remaining)
                try:
                    data, _ = s.recvfrom(4096)
                except socket.timeout:
                    break
                text = data.decode(errors="ignore")
                headers = dict(re.findall(r"^([\w-]+):\s*(.*?)\s*$", text, re.M))
                loc = re.match(r"yeelight://([\d.]+):(\d+)", headers.get("Location", ""))
                if not loc:
                    continue
                found[headers.get("id", loc.group(1))] = {
                    "ip": loc.group(1),
                    "port": int(loc.group(2)),
                    "model": headers.get("model"),
                    "fw_ver": headers.get("fw_ver"),
                    "power": headers.get("power"),
                    "support": headers.get("support", "").split(),
                    "found_via": local,
                }
        except OSError as exc:
            print(f"  (interface {local}: {exc})")
        finally:
            s.close()
    return found


def resolve_ip(args):
    if args.ip:
        return args.ip
    print("Discovering...")
    devices = discover()
    if not devices:
        sys.exit("No Yeelight found. Pass --ip, check LAN Control and that PC and lamp share a subnet.")
    lamps = {k: v for k, v in devices.items() if v["model"] == "lamp15"}
    pool = lamps or devices
    dev_id, dev = next(iter(pool.items()))
    print(f"Using {dev['model']} {dev['ip']} (id {dev_id})")
    return dev["ip"]


# ----------------------------------------------------------------------
# TCP client: line-buffered, matches replies by id, never closes on timeout
# ----------------------------------------------------------------------

class Lamp:
    def __init__(self, ip, timeout=1.0):
        self.ip = ip
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.next_id = 1
        self.side_messages = []  # notifications and stale replies

    def connect(self):
        self.sock = socket.create_connection((self.ip, TCP_PORT), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _read_message(self, deadline):
        while b"\r\n" not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("lamp closed the TCP connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError:
            return {"_unparsed": line.decode(errors="ignore")}

    def call(self, method, params, timeout=None):
        """Returns (reply_or_None, rtt_seconds). None = no reply in time."""
        if self.sock is None:
            self.connect()
        rid = self.next_id
        self.next_id += 1
        t0 = time.perf_counter()
        self.sock.sendall(dumps({"id": rid, "method": method, "params": params}))
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            msg = self._read_message(deadline)
            if msg is None:
                return None, time.perf_counter() - t0
            if msg.get("id") == rid:
                return msg, time.perf_counter() - t0
            self.side_messages.append(msg)

    def segments(self, left, right, **kw):
        return self.call("set_segment_rgb", [rgb_int(left), rgb_int(right)], **kw)

    def get_props(self, *names):
        reply, _ = self.call("get_prop", list(names))
        return reply.get("result") if reply else None


def show(label, reply, rtt):
    status = json.dumps(reply, separators=(",", ":")) if reply else "NO REPLY"
    print(f"  {label:<28} {rtt * 1000:7.1f} ms  {status}")


def ensure_on(lamp):
    lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
    lamp.call("bg_set_bright", [60, "sudden", 0])
    time.sleep(0.5)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def cmd_discover(args):
    devices = discover(timeout=args.timeout)
    if not devices:
        print("Nothing found.")
        return
    for dev_id, d in devices.items():
        print(f"id={dev_id} model={d['model']} fw={d['fw_ver']} ip={d['ip']}:{d['port']} "
              f"power={d['power']} (found via {d['found_via']})")
        print(f"   support: {' '.join(d['support'])}")


def cmd_info(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    props = ["power", "bright", "ct", "rgb", "hue", "sat", "color_mode",
             "bg_power", "bg_bright", "bg_rgb", "bg_ct", "bg_hue", "bg_sat",
             "bg_lmode", "bg_flowing", "name"]
    for p in props:  # one by one: unknown props must not poison the others
        reply, rtt = lamp.call("get_prop", [p])
        show(p, reply, rtt)
    lamp.close()


def cmd_rtt(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip, timeout=1.0)
    ensure_on(lamp)
    times, misses = [], 0
    for i in range(args.count):
        left, right = (RED, BLUE) if i % 2 == 0 else (BLUE, RED)
        reply, rtt = lamp.segments(left, right)
        if reply is None:
            misses += 1
        else:
            times.append(rtt * 1000)
    print(f"sent {args.count}, replies {len(times)}, timeouts {misses}")
    if times:
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        print(f"RTT ms: min {times[0]:.1f}  median {statistics.median(times):.1f}  "
              f"p95 {p95:.1f}  max {times[-1]:.1f}")
        print(f"max sequential rate: ~{1000 / statistics.mean(times):.1f} cmd/s")
    if lamp.side_messages:
        print(f"{len(lamp.side_messages)} unsolicited/stale messages, first: {lamp.side_messages[0]}")
    lamp.close()


def cmd_segments(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    ensure_on(lamp)

    print("\nA) COLD: bg_set_rgb first, then segments immediately (state reset?)")
    reply, rtt = lamp.call("bg_set_rgb", [rgb_int(WHITE), "sudden", 0])
    show("bg_set_rgb white", reply, rtt)
    reply, rtt = lamp.segments(RED, BLUE)
    show("segments L=red R=blue", reply, rtt)
    print("  -> WATCH: left red + right blue? or white/mixed?")
    time.sleep(2.5)

    print("\nB) WARM: patterns, each sent 3 times (idempotent?)")
    patterns = [
        ("L=red R=blue", RED, BLUE),
        ("L=blue R=red", BLUE, RED),
        ("L=green R=magenta", GREEN, MAGENTA),
        ("L=white R=red", WHITE, RED),
        ("L=red R=red", RED, RED),
    ]
    for name, left, right in patterns:
        for _ in range(3):
            reply, rtt = lamp.segments(left, right)
        show(name, reply, rtt)
        print("  -> WATCH and note what you see")
        time.sleep(2.0)

    print("\nC) Same pattern re-sent 30x at max TCP speed: does it drift to white?")
    for _ in range(30):
        reply, rtt = lamp.segments(RED, BLUE)
    show("30x red/blue", reply, rtt)
    time.sleep(2.0)
    print("props after test:", lamp.get_props("bg_power", "bg_bright", "bg_rgb", "bg_lmode"))
    lamp.close()


def cmd_gray(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    ensure_on(lamp)
    print("Left = test color, right = pure red reference. 2.5 s each.")
    tests = [
        ("white 255", (255, 255, 255)),
        ("gray 128", (128, 128, 128)),
        ("dark gray 30", (30, 30, 30)),
        ("near black 1", (1, 1, 1)),
        ("pink 255,128,128", (255, 128, 128)),
        ("dark pink 2,1,1", (2, 1, 1)),
        ("dim red 60,0,0", (60, 0, 0)),
        ("0,0,0", (0, 0, 0)),
    ]
    for name, color in tests:
        reply, rtt = lamp.segments(color, RED)
        show(name, reply, rtt)
        time.sleep(2.5)
    print("\nHypothesis: only hue/saturation matter, magnitude is normalised.")
    print("If so, 'dark gray 30' and 'near black 1' look identical to 'white 255'.")
    lamp.close()


# ----- UDP ------------------------------------------------------------

class UdpSession:
    def __init__(self, ip):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)
        self.token = None
        self.next_id = 100

    def open(self):
        self.sock.sendto(dumps({"id": 1, "method": "udp_sess_new", "params": []}),
                         (self.ip, UDP_PORT))
        data, _ = self.sock.recvfrom(4096)
        reply = json.loads(data.decode())
        print("  session reply:", json.dumps(reply, separators=(",", ":")))
        params = reply.get("params")
        if isinstance(params, dict) and params.get("token"):
            self.token = params["token"]
        elif isinstance(reply.get("result"), list) and reply["result"]:
            self.token = reply["result"][0]
        if not self.token:
            raise RuntimeError("no token in session reply")

    def send(self, method, params, wait=0.3):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params, "token": self.token}
        self.sock.sendto(dumps(msg), (self.ip, UDP_PORT))
        if not wait:
            return None
        self.sock.settimeout(wait)
        try:
            data, _ = self.sock.recvfrom(4096)
            return data.decode(errors="ignore").strip()
        except socket.timeout:
            return None

    def keepalive(self):
        return self.send("udp_sess_keep_alive", ["keeplive_interval", "10"])


def cmd_udp(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    ensure_on(lamp)
    udp = UdpSession(ip)
    udp.open()
    print("  keepalive reply:", udp.keepalive())

    l, r = rgb_int(RED), rgb_int(BLUE)
    l2, r2 = rgb_int(BLUE), rgb_int(RED)
    experiments = [
        ("seg [L,R] red/blue", "set_segment_rgb", [l, r]),
        ("seg [L,R] blue/red", "set_segment_rgb", [l2, r2]),
        ("seg [L,R,sudden,0]", "set_segment_rgb", [l, r, "sudden", 0]),
        ("bg_set_rgb green", "bg_set_rgb", [rgb_int(GREEN), "sudden", 0]),
        ("bg_set_bright 20", "bg_set_bright", [20, "sudden", 0]),
        ("bg_set_bright 100", "bg_set_bright", [100, "sudden", 0]),
    ]
    for name, method, params in experiments:
        reply = udp.send(method, params)
        props = lamp.get_props("bg_rgb", "bg_bright")
        print(f"  {name:<22} udp reply: {reply!s:<60} tcp props: {props}")
        print("    -> WATCH the lamp")
        time.sleep(1.5)
    print("\nIf props changed after a UDP command, that command is accepted over UDP.")
    print("set_segment_rgb may not change props at all, so rely on what you SEE.")
    lamp.close()


def cmd_udpfps(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    ensure_on(lamp)
    udp = UdpSession(ip)
    udp.open()
    period = 1.0 / args.fps
    sent = 0
    start = time.perf_counter()
    next_t = start
    print(f"Sending set_segment_rgb over UDP at {args.fps} fps for {args.seconds}s, "
          "alternating red/blue sides. WATCH for smoothness / dropped frames.")
    while time.perf_counter() - start < args.seconds:
        pair = (RED, BLUE) if sent % 2 == 0 else (BLUE, RED)
        udp.send("set_segment_rgb", [rgb_int(pair[0]), rgb_int(pair[1])], wait=0)
        sent += 1
        next_t += period
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    elapsed = time.perf_counter() - start
    print(f"sent {sent} frames in {elapsed:.2f}s = {sent / elapsed:.1f} fps")
    lamp.close()


def cmd_wake(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    ensure_on(lamp)
    for i in range(args.count):
        print(f"\nCycle {i + 1}")
        reply, rtt = lamp.call("bg_set_power", ["off", "sudden", 0], timeout=3.0)
        show("bg_set_power off", reply, rtt)
        time.sleep(2.0)
        reply, rtt = lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
        show("bg_set_power on", reply, rtt)
        reply, rtt = lamp.segments(RED, BLUE, timeout=3.0)
        show("segments right after on", reply, rtt)
        print("  props:", lamp.get_props("bg_power", "bg_bright", "bg_rgb"))
        time.sleep(1.5)
    print("\nIf 'power on' RTT is > 250 ms, the main script's 0.25 s socket timeout")
    print("closes the connection mid-wake. For the true optical delay, film the lamp")
    print("with a phone in slow-motion and count frames.")
    lamp.close()


def cmd_chroma(args):
    """Ask the lamp what udp_chroma_sess_new returns (UDP and TCP). Only opens a session."""
    ip = resolve_ip(args)
    print("UDP 55444: udp_chroma_sess_new")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)
    s.sendto(dumps({"id": 1, "method": "udp_chroma_sess_new", "params": []}), (ip, UDP_PORT))
    try:
        data, addr = s.recvfrom(4096)
        print("  reply from", addr)
        print("  text:", data.decode(errors="replace").strip())
        print("  hex :", data.hex())
    except socket.timeout:
        print("  no reply")
    s.close()
    print("TCP 55443: udp_chroma_sess_new")
    lamp = Lamp(ip)
    reply, rtt = lamp.call("udp_chroma_sess_new", [])
    show("udp_chroma_sess_new", reply, rtt)
    lamp.close()
    print("\nPaste the output back. Do not post session tokens in public.")


def cmd_dark(args):
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
    print("Segments (0,0,0) on both sides, global brightness stepping down.")
    print("Note at which step the residual glow becomes acceptable / invisible.")
    for bright in (100, 60, 30, 10, 3, 1):
        lamp.call("bg_set_bright", [bright, "sudden", 0])
        reply, rtt = lamp.segments((0, 0, 0), (0, 0, 0))
        show(f"bright {bright}, seg 0,0,0", reply, rtt)
        time.sleep(2.0)
    print("\nSame steps with L=red R=blue: is a dim color still clean and distinct?")
    for bright in (30, 10, 3, 1):
        lamp.call("bg_set_bright", [bright, "sudden", 0])
        reply, rtt = lamp.segments(RED, BLUE)
        show(f"bright {bright}, red/blue", reply, rtt)
        time.sleep(2.0)
    lamp.call("bg_set_bright", [60, "sudden", 0])
    lamp.close()


def cmd_poweroff(args):
    """Why does the light stay on after bg_set_power off? Isolates TCP-only vs UDP session vs UDP frames."""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    names = ["power", "bright", "bg_power", "bg_bright", "bg_lmode"]

    def snap(label):
        values = lamp.get_props(*names) or []
        print(f"  props after {label}: {dict(zip(names, values))}")

    def tcp_power(on, label):
        reply, rtt = lamp.call("bg_set_power", ["on" if on else "off", "sudden", 0], timeout=3.0)
        show(label, reply, rtt)
        time.sleep(3.0 * k)
        snap(label)

    def watch(question):
        print(f"  -> WATCH: {question}")

    l, r = rgb_int(RED), rgb_int(BLUE)

    print("\nA) BASELINE, TCP only: segments over TCP, then bg_set_power off over TCP")
    tcp_power(True, "TCP bg_set_power on")
    lamp.call("bg_set_bright", [60, "sudden", 0])
    lamp.segments(RED, BLUE)
    time.sleep(1.5 * k)
    snap("TCP segments")
    tcp_power(False, "TCP bg_set_power off")
    watch("is the light DARK? If not: does the light come from the rear (wall) or the front (desk)?")

    print("\nB) UDP session opened, but NO frames sent, then TCP off")
    tcp_power(True, "TCP bg_set_power on")
    udp = UdpSession(ip)
    udp.open()
    time.sleep(1.0 * k)
    tcp_power(False, "TCP bg_set_power off (session open)")
    watch("dark?")

    print("\nC) UDP frames (segments + brightness), then TCP off")
    tcp_power(True, "TCP bg_set_power on")
    udp.send("set_segment_rgb", [l, r], wait=0)
    udp.send("bg_set_bright", [60, "sudden", 0], wait=0)
    time.sleep(1.5 * k)
    snap("UDP frames")
    tcp_power(False, "TCP bg_set_power off (after UDP frames)")
    watch("dark? (this is exactly what the new script does)")

    print("\nD) UDP frames, then power off sent over UDP too")
    tcp_power(True, "TCP bg_set_power on")
    udp.send("set_segment_rgb", [l, r], wait=0)
    udp.send("bg_set_bright", [60, "sudden", 0], wait=0)
    time.sleep(1.5 * k)
    reply = udp.send("bg_set_power", ["off", "sudden", 0], wait=0.3)
    print(f"  UDP bg_set_power off reply: {reply}")
    time.sleep(3.0 * k)
    snap("UDP bg_set_power off")
    watch("dark?")

    print("\nE) Leaving the lamp off over TCP")
    tcp_power(False, "TCP bg_set_power off")
    lamp.close()
    print("\nFor each step A-D note: dark / still lit (rear or front). Paste the output.")


def cmd_raw(args):
    """Send one arbitrary command, then show the main/bg power state. Example: raw set_power off sudden 0"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    params = [int(x) if re.fullmatch(r"-?\d+", x) else x for x in args.params]
    reply, rtt = lamp.call(args.method, params, timeout=3.0)
    show(f"{args.method} {params}", reply, rtt)
    time.sleep(args.wait)
    names = ["power", "bright", "bg_power", "bg_bright"]
    values = lamp.get_props(*names) or []
    print("  props:", dict(zip(names, values)))
    lamp.close()


def cmd_maintest(args):
    """Does the rear light still obey commands after the front light was switched off with set_power off?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    names = ["power", "bg_power", "bg_bright", "bg_rgb"]

    def step(label, method, params, question=None):
        reply, rtt = lamp.call(method, params, timeout=3.0)
        show(label, reply, rtt)
        time.sleep(2.5 * k)
        values = lamp.get_props(*names) or []
        print(f"      lamp says: {dict(zip(names, values))}")
        if question:
            print(f"      -> WATCH: {question}")

    off = ["off", "sudden", 0]
    on = ["on", "sudden", 0]

    print("\n1) Front (desk) light ON: baseline")
    step("set_power on", "set_power", on)
    step("bg_set_power on", "bg_set_power", on)
    step("bg_set_rgb red", "bg_set_rgb", [rgb_int(RED), "sudden", 0], "rear light RED?")
    step("bg_set_power off", "bg_set_power", off, "rear light OFF?")

    print("\n2) Front light switched OFF with set_power off, rear controlled over TCP")
    step("set_power off", "set_power", off, "desk light OFF?")
    step("bg_set_power on", "bg_set_power", on, "rear light ON?")
    step("bg_set_rgb red", "bg_set_rgb", [rgb_int(RED), "sudden", 0], "rear light RED?")
    step("bg_set_bright 20", "bg_set_bright", [20, "sudden", 0], "rear light dimmer?")
    step("bg_set_power off", "bg_set_power", off, "rear light OFF?  (this is the step that fails in the script)")

    print("\n3) Front light still off, rear controlled over a UDP session")
    step("bg_set_power on", "bg_set_power", on, "rear light ON?")
    udp = UdpSession(ip)
    udp.open()
    udp.send("set_segment_rgb", [rgb_int(RED), rgb_int(BLUE)], wait=0)
    udp.send("bg_set_bright", [40, "sudden", 0], wait=0)
    time.sleep(2.5 * k)
    values = lamp.get_props(*names) or []
    print(f"      lamp says: {dict(zip(names, values))}")
    print("      -> WATCH: left red / right blue at medium brightness?")
    step("bg_set_power off", "bg_set_power", off, "rear light OFF?")

    print("\n4) Restoring the front light")
    step("set_power on", "set_power", on, "desk light back ON?")
    lamp.close()
    print("\nFor each WATCH line note yes / no, and paste the whole output.")


def spin_sleep(seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def cmd_udpgap(args):
    """Does the lamp drop a UDP datagram that follows another one too closely?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    ensure_on(lamp)
    udp = UdpSession(ip)
    udp.open()
    l, r = rgb_int(RED), rgb_int(BLUE)
    n = args.repeats
    print("set_segment_rgb, wait GAP, bg_set_bright over UDP; then the lamp's bg_bright is read over TCP.")
    print("A gap row counts the runs in which the brightness command was applied.\n")
    print(" gap ms | applied")
    for gap_ms in (None, 0, 2, 5, 10, 20, 40, 80, 160):
        applied = 0
        for i in range(n):
            target = 25 if i % 2 == 0 else 75
            lamp.call("bg_set_bright", [50, "sudden", 0])
            time.sleep(0.2)
            if gap_ms is not None:
                udp.send("set_segment_rgb", [l, r], wait=0)
                spin_sleep(gap_ms / 1000.0)
            udp.send("bg_set_bright", [target, "sudden", 0], wait=0)
            time.sleep(0.4)
            values = lamp.get_props("bg_bright") or []
            if values and values[0] == str(target):
                applied += 1
        label = "alone" if gap_ms is None else str(gap_ms)
        print(f"{label:>7} | {applied}/{n}")
    print("\n'alone' = brightness command without a segment command before it (control row).")
    lamp.close()


def cmd_segbright(args):
    """Which commands still change the rear light's brightness once segment mode is active?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    names = ["bg_power", "bg_lmode", "bg_bright", "bg_rgb"]
    udp = None
    l, r = rgb_int(RED), rgb_int(BLUE)

    def props():
        return dict(zip(names, lamp.get_props(*names) or []))

    def tcp(label, method, params, question=None):
        reply, rtt = lamp.call(method, params, timeout=3.0)
        show(label + " [TCP]", reply, rtt)
        time.sleep(2.0 * k)
        print(f"      lamp says: {props()}")
        if question:
            print(f"      -> WATCH: {question}")

    def via_udp(label, method, params, question=None):
        udp.send(method, params, wait=0)
        print(f"  {label + ' [UDP]':<34} sent")
        time.sleep(2.0 * k)
        print(f"      lamp says: {props()}")
        if question:
            print(f"      -> WATCH: {question}")

    print("\n0) Baseline: plain color mode, brightness 50")
    tcp("bg_set_power on", "bg_set_power", ["on", "sudden", 0])
    tcp("bg_set_rgb white", "bg_set_rgb", [rgb_int(WHITE), "sudden", 0])
    tcp("bg_set_bright 50", "bg_set_bright", [50, "sudden", 0])
    udp = UdpSession(ip)
    udp.open()

    print("\n1) Control: UDP brightness BEFORE segment mode")
    via_udp("bg_set_bright 20", "bg_set_bright", [20, "sudden", 0], "clearly dimmer than before?")

    print("\n2) Enter segment mode (left red, right blue)")
    tcp("set_segment_rgb red/blue", "set_segment_rgb", [l, r], "left red, right blue?")

    print("\n3) In segment mode: brightness over TCP")
    tcp("bg_set_bright 90", "bg_set_bright", [90, "sudden", 0], "brighter than step 2?")
    tcp("bg_set_bright 10", "bg_set_bright", [10, "sudden", 0], "clearly dimmer?")

    print("\n4) In segment mode: brightness over UDP")
    via_udp("bg_set_bright 90", "bg_set_bright", [90, "sudden", 0], "brighter?")
    via_udp("bg_set_bright 10", "bg_set_bright", [10, "sudden", 0], "dimmer?")

    print("\n5) In segment mode: relative brightness commands")
    tcp("bg_set_bright 50", "bg_set_bright", [50, "sudden", 0])
    tcp("bg_adjust_bright +40", "bg_adjust_bright", [40, 0], "brighter?")
    tcp("bg_adjust_bright -40", "bg_adjust_bright", [-40, 0], "dimmer?")

    print("\n6) Back to plain color mode with one UDP bg_set_rgb, then UDP brightness")
    via_udp("bg_set_rgb white", "bg_set_rgb", [rgb_int(WHITE), "sudden", 0], "plain white now (segments gone)?")
    via_udp("bg_set_bright 20", "bg_set_bright", [20, "sudden", 0], "dimmer?")

    print("\n7) Color AND brightness in one command (scene), over TCP")
    tcp("bg_set_scene color red 30", "bg_set_scene", ["color", rgb_int(RED), 30], "dim red?")
    tcp("set_segment_rgb red/blue", "set_segment_rgb", [l, r], "split again?")
    tcp("bg_set_scene color red 90", "bg_set_scene", ["color", rgb_int(RED), 90], "bright red? (all red, no split)")

    print("\n8) Cleanup: bg off")
    tcp("bg_set_power off", "bg_set_power", ["off", "sudden", 0])
    lamp.close()
    print("\nPaste the whole output. For each WATCH line add: yes / no / a little.")


def cmd_brightstream(args):
    """Does a brightness change survive when the UDP color stream resumes? (the slider case)"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    l, r = rgb_int(RED), rgb_int(BLUE)
    reply, _ = lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
    if not reply or "result" not in reply:
        sys.exit(f"Lamp refused the command ({reply}). Wait one minute (TCP quota) and run again.")
    udp = UdpSession(ip)
    udp.open()

    def props():
        return dict(zip(["bg_bright", "bg_rgb"], lamp.get_props("bg_bright", "bg_rgb") or []))

    def stream(seconds, label):
        """Frames like the app: the same segments again every 0.5 s."""
        end = time.time() + seconds * k
        while time.time() < end:
            udp.send("set_segment_rgb", [l, r], wait=0)
            time.sleep(0.5)
        print(f"  {label}")

    def set_bright(value):
        time.sleep(1.5 * k)                       # quiet period, like the app
        reply, _ = lamp.call("bg_set_bright", [value, "sudden", 0], timeout=3.0)
        time.sleep(0.3)
        print(f"  TCP bg_set_bright {value}: {reply}  lamp says {props()}")

    print("Left red, right blue. Answer each WATCH with: bright / dim.\n")
    set_bright(100)
    stream(3, "A) streaming at brightness 100   -> WATCH: bright?")
    set_bright(20)
    time.sleep(2.0 * k)
    print("  B) stream paused, brightness 20     -> WATCH: dim?")
    stream(4, "C) stream resumed (every 0.5 s)    -> WATCH: still dim, or bright again?")
    set_bright(100)
    time.sleep(2.0 * k)
    set_bright(20)
    time.sleep(1.5 * k)
    udp.send("set_segment_rgb", [l, r], wait=0)
    time.sleep(4.0 * k)
    print("  D) ONE UDP frame after brightness 20 -> WATCH: dim?")
    set_bright(100)
    set_bright(20)
    time.sleep(1.5 * k)
    lamp.call("set_segment_rgb", [l, r], timeout=3.0)
    time.sleep(4.0 * k)
    print("  E) ONE TCP frame after brightness 20 -> WATCH: dim?")
    set_bright(100)
    set_bright(20)
    time.sleep(1.5 * k)
    end = time.time() + 5.0 * k
    n = 0
    while time.time() < end:                      # like the app: 30 frames/s, colors drifting
        v = 200 + (n % 2) * 40
        udp.send("set_segment_rgb", [rgb_int((v, 0, 0)), rgb_int((0, 0, v))], wait=0)
        n += 1
        time.sleep(1 / 30)
    print("  F) 30 frames/s stream after brightness 20 -> WATCH: dim, or bright again (when)?")
    for label, jitter in (("G) IDENTICAL frame every 0.5 s", False), ("H) frame every 0.5 s, color changes by 1 each time", True)):
        set_bright(100)
        set_bright(20)
        time.sleep(1.5 * k)
        end = time.time() + 6.0 * k
        n = 0
        while time.time() < end:
            v = 255 - (n % 2 if jitter else 0)
            udp.send("set_segment_rgb", [rgb_int((v, 0, 0)), rgb_int((0, 0, v))], wait=0)
            n += 1
            time.sleep(0.5)
        print(f"  {label} after brightness 20 -> WATCH: dim all the time, or bright again (after how many seconds)?")
    lamp.call("bg_set_power", ["off", "sudden", 0])
    lamp.close()
    print("\nPaste the output with your answers for A-H.")


def cmd_segmix(args):
    """After segments are sent over UDP, which brightness routes still work?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    names = ["bg_power", "bg_bright", "bg_rgb"]
    l, r = rgb_int(RED), rgb_int(BLUE)

    def props():
        return dict(zip(names, lamp.get_props(*names) or []))

    def tcp(label, method, params, question=None):
        reply, rtt = lamp.call(method, params, timeout=3.0)
        show(label + " [TCP]", reply, rtt)
        time.sleep(2.0 * k)
        print(f"      lamp says: {props()}")
        if question:
            print(f"      -> WATCH: {question}")

    def via_udp(session, label, method, params, question=None, extra_wait=0.0):
        session.send(method, params, wait=0)
        print(f"  {label + ' [UDP]':<38} sent")
        time.sleep((2.0 + extra_wait) * k)
        print(f"      lamp says: {props()}")
        if question:
            print(f"      -> WATCH: {question}")

    print("\n0) Baseline: plain color mode, brightness 50")
    tcp("bg_set_power on", "bg_set_power", ["on", "sudden", 0])
    tcp("bg_set_rgb white", "bg_set_rgb", [rgb_int(WHITE), "sudden", 0])
    tcp("bg_set_bright 50", "bg_set_bright", [50, "sudden", 0])
    udp = UdpSession(ip)
    udp.open()

    print("\n1) Segments over UDP (what the main program does)")
    via_udp(udp, "set_segment_rgb red/blue", "set_segment_rgb", [l, r], "left red, right blue?")

    print("\n2) Brightness over TCP after UDP segments")
    tcp("bg_set_bright 90", "bg_set_bright", [90, "sudden", 0], "brighter?")
    tcp("bg_set_bright 10", "bg_set_bright", [10, "sudden", 0], "clearly dimmer?")

    print("\n3) Brightness over UDP after UDP segments (the failing case), and a late re-read")
    via_udp(udp, "bg_set_bright 90", "bg_set_bright", [90, "sudden", 0], "brighter?")
    time.sleep(3.0 * k)
    print(f"      lamp says 3 s later: {props()}")

    print("\n4) Relative brightness over UDP")
    via_udp(udp, "bg_adjust_bright -40", "bg_adjust_bright", [-40, 0], "dimmer?")

    print("\n5) Repeated UDP segments, then TCP brightness again")
    for _ in range(5):
        udp.send("set_segment_rgb", [l, r], wait=0)
        time.sleep(0.1)
    tcp("bg_set_bright 30", "bg_set_bright", [30, "sudden", 0], "dimmer?")

    print("\n6) A SECOND UDP session sends the brightness (is the lock per session?)")
    udp2 = UdpSession(ip)
    udp2.open()
    via_udp(udp2, "bg_set_bright 80 (session 2)", "bg_set_bright", [80, "sudden", 0], "brighter?")

    print("\n7) Cleanup: bg off")
    tcp("bg_set_power off", "bg_set_power", ["off", "sudden", 0])
    lamp.close()
    print("\nPaste the whole output. For each WATCH line add: yes / no / a little.")


def cmd_tcpsustain(args):
    """Can set_segment_rgb run over TCP at a steady frame rate for a long time without quota errors?"""
    import colorsys
    ip = resolve_ip(args)
    lamp = Lamp(ip, timeout=1.0)
    ensure_on(lamp)
    period = 1.0 / args.fps
    start = time.perf_counter()
    next_t = start
    sent = timeouts = 0
    errors = {}
    rtts = []
    last_report = start
    window = [0, 0, 0]  # sent, ok, bad in the current 5 s window
    print(f"set_segment_rgb over TCP at {args.fps} fps for {args.seconds}s (rainbow, watch for smoothness).\n")
    while time.perf_counter() - start < args.seconds:
        hue = (sent * 0.004) % 1.0
        c1 = tuple(int(255 * v) for v in colorsys.hsv_to_rgb(hue, 1, 1))
        c2 = tuple(int(255 * v) for v in colorsys.hsv_to_rgb((hue + 0.5) % 1.0, 1, 1))
        reply, rtt = lamp.segments(c1, c2)
        sent += 1
        window[0] += 1
        if reply is None:
            timeouts += 1
            window[2] += 1
        elif "error" in reply:
            msg = json.dumps(reply["error"], separators=(",", ":"))
            errors[msg] = errors.get(msg, 0) + 1
            window[2] += 1
        else:
            rtts.append(rtt * 1000)
            window[1] += 1
        now = time.perf_counter()
        if now - last_report >= 5.0:
            print(f"  t={now - start:5.1f}s  sent {window[0]:4d}  ok {window[1]:4d}  failed {window[2]:4d}")
            window = [0, 0, 0]
            last_report = now
        next_t += period
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    elapsed = time.perf_counter() - start
    print(f"\nsent {sent} in {elapsed:.1f}s = {sent / elapsed:.1f} fps, ok {len(rtts)}, timeouts {timeouts}")
    print(f"errors: {errors if errors else 'none'}")
    if rtts:
        rtts.sort()
        print(f"RTT ms: median {statistics.median(rtts):.1f}  p95 {rtts[int(len(rtts) * 0.95) - 1]:.1f}  max {rtts[-1]:.1f}")
    lamp.close()


def read_bright_or_exit(lamp):
    reply, _ = lamp.call("get_prop", ["bg_bright"])
    if not reply or "result" not in reply:
        sys.exit(f"Lamp refused get_prop ({reply}). TCP quota is probably used up: wait one minute and run again.")
    return reply["result"][0]


def cmd_lock(args):
    """How long after a UDP set_segment_rgb is bg_set_bright ignored, and does 'sudden, 0' change that?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    reply, _ = lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
    if not reply or "result" not in reply:
        sys.exit(f"Lamp refused the command ({reply}). Wait one minute (TCP quota) and run again.")
    udp = UdpSession(ip)
    udp.open()
    l, r = rgb_int(RED), rgb_int(BLUE)
    gaps = [int(g) for g in args.gaps.split(",")]
    n = args.repeats

    def seg_plain():
        udp.send("set_segment_rgb", [l, r], wait=0)

    def seg_sudden():
        udp.send("set_segment_rgb", [l, r, "sudden", 0], wait=0)

    variants = [("UDP segments [l, r]", seg_plain),
                ("UDP segments [l, r, 'sudden', 0]", seg_sudden)]
    if args.plain_only:
        variants = variants[:1]
    print("Each trial: UDP segment command, wait GAP, UDP bg_set_bright (alternating 25 / 75),")
    print("then the lamp's bg_bright is read. 'applied' = the brightness command took effect.\n")
    for name, seg in variants:
        lamp.call("bg_set_bright", [50, "sudden", 0])
        time.sleep(3.5)  # let any lock from the previous variant expire
        print(f"{name}")
        if "sudden" in name:
            seg()
            print("   -> WATCH once: is left red / right blue still shown with these parameters?")
            time.sleep(2.5)
        row = []
        for gap_ms in gaps:
            applied = 0
            for i in range(n):
                target = 25 if i % 2 == 0 else 75
                seg()
                spin_sleep(gap_ms / 1000.0)
                udp.send("bg_set_bright", [target, "sudden", 0], wait=0)
                time.sleep(0.4)
                if read_bright_or_exit(lamp) == str(target):
                    applied += 1
            row.append(f"{gap_ms}ms {applied}/{n}")
        print("   " + "  |  ".join(row) + "\n")
    lamp.call("bg_set_power", ["off", "sudden", 0])
    lamp.close()
    print("Paste the output.")


def cmd_udpmag(args):
    """Do UDP segment colors honour their brightness (RGB magnitude)? TCP segments ignore it."""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    reply, _ = lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
    if not reply or "result" not in reply:
        sys.exit(f"Lamp refused the command ({reply}). Wait one minute (TCP quota) and run again.")
    lamp.call("bg_set_bright", [60, "sudden", 0])
    udp = UdpSession(ip)
    udp.open()
    print("UDP segments. LEFT = red, RIGHT = white, both at the given level (0-255). 3 s each.")
    print("WATCH whether the light gets dimmer with the level, and what happens at 0.\n")
    for v in (255, 128, 64, 32, 12, 4, 0):
        pair = [rgb_int((v, 0, 0)), rgb_int((v, v, v))]
        for _ in range(3):  # UDP can lose a datagram
            udp.send("set_segment_rgb", pair, wait=0)
            time.sleep(0.05)
        print(f"  level {v:3d} sent  -> dimmer than the previous level? (0 = completely dark?)")
        time.sleep(3.0 * k)
    lamp.call("bg_set_power", ["off", "sudden", 0])
    lamp.close()
    print("\nFor each level answer: same / dimmer / dark. Paste the output with your notes.")


def cmd_frontlock(args):
    """Is the FRONT (desk) light's brightness also ignored right after a UDP segment command?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    for method in ("set_power", "bg_set_power"):
        reply, _ = lamp.call(method, ["on", "sudden", 0], timeout=3.0)
        if not reply or "result" not in reply:
            sys.exit(f"Lamp refused {method} ({reply}). Wait one minute (TCP quota) and run again.")
    udp = UdpSession(ip)
    udp.open()
    l, r = rgb_int(RED), rgb_int(BLUE)
    gaps = [0, 150, 400, 800, 1500]
    n = args.repeats
    print("UDP segment command, wait GAP, then TCP set_bright on the FRONT light (alternating 30 / 80);")
    print("the lamp's 'bright' property is read afterwards.\n")
    row = []
    for gap_ms in gaps:
        applied = 0
        for i in range(n):
            target = 30 if i % 2 == 0 else 80
            udp.send("set_segment_rgb", [l, r], wait=0)
            spin_sleep(gap_ms / 1000.0)
            lamp.call("set_bright", [target, "sudden", 0])
            time.sleep(0.4)
            reply, _ = lamp.call("get_prop", ["bright"])
            if not reply or "result" not in reply:
                sys.exit(f"Lamp refused get_prop ({reply}). Wait one minute (TCP quota) and run again.")
            if reply["result"][0] == str(target):
                applied += 1
        row.append(f"{gap_ms}ms {applied}/{n}")
    print("  " + "  |  ".join(row))
    print("\n'applied' = the front light's brightness command took effect.")
    lamp.call("bg_set_power", ["off", "sudden", 0])
    lamp.close()


def cmd_frontct(args):
    """Front light color temperature: which range does set_ct_abx accept, and is it also ignored after a segment?"""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    k = args.scale
    names = ["power", "bright", "ct", "color_mode"]

    def props():
        return dict(zip(names, lamp.get_props(*names) or []))

    before = props()
    print("Before:", before)
    reply, _ = lamp.call("set_power", ["on", "sudden", 0], timeout=3.0)
    if not reply or "result" not in reply:
        sys.exit(f"Lamp refused set_power ({reply}). Wait one minute (TCP quota) and run again.")
    time.sleep(1.0 * k)

    print("\nA) Range: set_ct_abx over TCP (an error reply means the value is out of range)")
    for value in (1700, 2000, 2700, 3500, 4500, 5500, 6500, 7000):
        reply, rtt = lamp.call("set_ct_abx", [value, "sudden", 0], timeout=3.0)
        show(f"set_ct_abx {value}", reply, rtt)
        time.sleep(1.5 * k)
        print(f"      lamp says: {props()}")
        print("      -> WATCH: warmer / colder than the previous step? (or the same)")

    print("\nB) Is the color temperature also ignored right after a UDP segment command?")
    udp = UdpSession(ip)
    udp.open()
    l, r = rgb_int(RED), rgb_int(BLUE)
    row = []
    for gap_ms in (0, 400, 800, 1500):
        applied = 0
        for i in range(2):
            target = 3000 if i % 2 == 0 else 5500
            udp.send("set_segment_rgb", [l, r], wait=0)
            spin_sleep(gap_ms / 1000.0)
            lamp.call("set_ct_abx", [target, "sudden", 0])
            time.sleep(0.4)
            reply, _ = lamp.call("get_prop", ["ct"])
            if not reply or "result" not in reply:
                sys.exit(f"Lamp refused get_prop ({reply}). Wait one minute (TCP quota) and run again.")
            if reply["result"][0] == str(target):
                applied += 1
        row.append(f"{gap_ms}ms {applied}/2")
    print("  " + "  |  ".join(row))

    old_ct = before.get("ct")
    if old_ct and old_ct.isdigit():
        lamp.call("set_ct_abx", [int(old_ct), "sudden", 0])
        print(f"\nRestored the color temperature to {old_ct} K.")
    lamp.close()
    print("\nPaste the output and say which WATCH lines were warmer / colder / the same.")


def cmd_dither(args):
    """Can brightness be emulated by alternating a color with (0,0,0) frames? You judge dimming and flicker."""
    ip = resolve_ip(args)
    lamp = Lamp(ip)
    reply, _ = lamp.call("bg_set_power", ["on", "sudden", 0], timeout=3.0)
    if not reply or "result" not in reply:
        sys.exit(f"Lamp refused the command ({reply}). Wait one minute (TCP quota) and run again.")
    lamp.call("bg_set_bright", [60, "sudden", 0])
    udp = UdpSession(ip)
    udp.open()
    color = [rgb_int(WHITE), rgb_int(RED)]       # left white, right red
    dark = [0, 0]
    if args.smooth > 0:                          # ask the lamp to fade between frames
        color = color + ["smooth", int(args.smooth)]
        dark = dark + ["smooth", int(args.smooth)]
    period = 1.0 / args.fps
    frames = int(args.fps * args.seconds)
    fade = f", fade {int(args.smooth)} ms" if args.smooth > 0 else ""
    print(f"UDP at {args.fps} fps{fade}; color frames alternate with (0,0,0) frames to emulate dimmer light.")
    print(f"{args.seconds} s per level. For each level judge: how bright (vs the previous level) and flicker (none / slight / strong).\n")
    for duty in (100, 75, 50, 25, 10, 0):
        acc = 0.0
        sent_color = 0
        start = time.perf_counter()
        for i in range(frames):
            acc += duty / 100.0
            if acc >= 0.9999:
                acc -= 1.0
                frame = color
                sent_color += 1
            else:
                frame = dark
            udp.send("set_segment_rgb", frame, wait=0)
            target = start + (i + 1) * period
            delay = target - time.perf_counter()
            if delay > 0.002:
                time.sleep(delay - 0.001)
            while time.perf_counter() < target:
                pass
        print(f"  duty {duty:3d}% ({sent_color}/{frames} color frames)  ->  brightness? flicker?")
    time.sleep(1.5)   # the lamp ignores power commands right after color frames
    lamp.call("bg_set_power", ["off", "sudden", 0])
    lamp.close()
    print("\nPaste the output with your notes for each level.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", help="lamp IP (skip discovery)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover")
    p.add_argument("--timeout", type=float, default=2.0)
    p.set_defaults(func=cmd_discover)

    sub.add_parser("info").set_defaults(func=cmd_info)

    p = sub.add_parser("rtt")
    p.add_argument("--count", type=int, default=60)
    p.set_defaults(func=cmd_rtt)

    sub.add_parser("segments").set_defaults(func=cmd_segments)
    sub.add_parser("gray").set_defaults(func=cmd_gray)
    sub.add_parser("udp").set_defaults(func=cmd_udp)
    sub.add_parser("chroma").set_defaults(func=cmd_chroma)
    sub.add_parser("dark").set_defaults(func=cmd_dark)
    p = sub.add_parser("dither")
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--seconds", type=float, default=4)
    p.add_argument("--smooth", type=float, default=0, help="fade time in ms added to every frame (0 = none)")
    p.set_defaults(func=cmd_dither)
    p = sub.add_parser("frontct")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_frontct)
    p = sub.add_parser("frontlock")
    p.add_argument("--repeats", type=int, default=2)
    p.set_defaults(func=cmd_frontlock)
    p = sub.add_parser("lock")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--gaps", default="0,150,400,800,1500,3000", help="comma separated gaps in milliseconds")
    p.add_argument("--plain-only", action="store_true", help="test only the plain [l, r] segment command")
    p.set_defaults(func=cmd_lock)
    p = sub.add_parser("udpmag")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_udpmag)
    p = sub.add_parser("segmix")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_segmix)
    p = sub.add_parser("tcpsustain")
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--seconds", type=float, default=30)
    p.set_defaults(func=cmd_tcpsustain)
    p = sub.add_parser("brightstream")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_brightstream)
    p = sub.add_parser("segbright")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_segbright)
    p = sub.add_parser("udpgap")
    p.add_argument("--repeats", type=int, default=6)
    p.set_defaults(func=cmd_udpgap)
    p = sub.add_parser("maintest")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_maintest)
    p = sub.add_parser("raw")
    p.add_argument("method")
    p.add_argument("params", nargs="*")
    p.add_argument("--wait", type=float, default=1.0)
    p.set_defaults(func=cmd_raw)
    p = sub.add_parser("poweroff")
    p.add_argument("--scale", type=float, default=1.0, help="multiply all waits (0.05 for tests)")
    p.set_defaults(func=cmd_poweroff)

    p = sub.add_parser("udpfps")
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--seconds", type=float, default=5)
    p.set_defaults(func=cmd_udpfps)

    p = sub.add_parser("wake")
    p.add_argument("--count", type=int, default=3)
    p.set_defaults(func=cmd_wake)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
