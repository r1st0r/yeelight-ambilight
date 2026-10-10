"""
yeelight_lan.py - minimal LAN client for the Yeelight Monitor Light Bar Pro (lamp15).

Standard library only:
  * discover()      SSDP discovery on every local interface
  * LampTCP         line-buffered TCP control (replies matched by id, never
                    closes the connection on a slow reply)
  * LampUDP         UDP realtime session (udp_sess_new + keepalive)
  * Connection      resolves the lamp by device id, so a DHCP change is survivable
"""

import json
import os
import re
import socket
import time
from typing import Dict, List, Optional, Tuple

TRACE = False  # set to True (--trace) to log every command and reply


def trace(message: str) -> None:
    if TRACE:
        now = time.time()
        print(f"[trace {time.strftime('%H:%M:%S', time.localtime(now))}.{int(now * 1000) % 1000:03d}] {message}")


TCP_PORT = 55443
UDP_PORT = 55444
SSDP_ADDR = ("239.255.255.250", 1982)
SSDP_MSG = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1982\r\n"
    b'MAN: "ssdp:discover"\r\n'
    b"ST: wifi_bulb\r\n\r\n"
)


def rgb_to_int(rgb: Tuple[int, int, int]) -> int:
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]


def _dumps(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\r\n").encode()


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def local_ips() -> List[str]:
    ips = set()
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(addrs)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # UDP connect: no packet is sent
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    ips = {ip for ip in ips if not ip.startswith("127.")}
    return sorted(ips) or ["0.0.0.0"]


def discover(timeout: float = 2.0) -> Dict[str, dict]:
    """Return {device_id: info}, searching from every local interface."""
    found: Dict[str, dict] = {}
    for local in local_ips():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.bind((local, 0))
            if local != "0.0.0.0":
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(local))
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
                }
        except OSError:
            pass  # interface without multicast, VPN adapter, etc.
        finally:
            s.close()
    return found


def pick_device(devices: Dict[str, dict],
                device_id: Optional[str] = None) -> Optional[Tuple[str, dict]]:
    if device_id and device_id in devices:
        return device_id, devices[device_id]
    for dev_id, dev in devices.items():
        if dev.get("model") == "lamp15" or "set_segment_rgb" in dev.get("support", []):
            return dev_id, dev
    return None


# ----------------------------------------------------------------------
# TCP control
# ----------------------------------------------------------------------

class LampTCP:
    def __init__(self, ip: str, timeout: float = 1.0):
        self.ip = ip
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self.next_id = 1

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""

    def _connect(self) -> None:
        self.sock = socket.create_connection((self.ip, TCP_PORT), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    def _read_message(self, deadline: float) -> Optional[dict]:
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
            return {}

    def call(self, method: str, params: list, timeout: Optional[float] = None) -> Optional[dict]:
        """Send a command and return the reply with the same id, or None on timeout.

        Notifications (method "props") and stale replies are skipped. A timeout
        does NOT close the connection; socket errors do, and are raised.
        """
        try:
            if self.sock is None:
                self._connect()
            rid = self.next_id
            self.next_id += 1
            t0 = time.monotonic()
            self.sock.sendall(_dumps({"id": rid, "method": method, "params": params}))
            deadline = time.monotonic() + (timeout or self.timeout)
            while True:
                msg = self._read_message(deadline)
                if msg is None:
                    trace(f"TCP {method} {params} -> NO REPLY")
                    return None
                if msg.get("id") == rid:
                    ms = (time.monotonic() - t0) * 1000
                    trace(f"TCP {method} {params} -> {json.dumps(msg, separators=(',', ':'))} ({ms:.0f} ms)")
                    return msg
        except OSError:
            self.close()
            raise

    def set_power(self, on: bool) -> bool:
        reply = self.call("bg_set_power", ["on" if on else "off", "sudden", 0], timeout=2.0)
        return bool(reply) and reply.get("result") == ["ok"]


# ----------------------------------------------------------------------
# UDP realtime session
# ----------------------------------------------------------------------

class LampUDP:
    def __init__(self, ip: str):
        self.ip = ip
        self.sock: Optional[socket.socket] = None
        self.token: Optional[str] = None
        self.next_id = 100
        self.last_alive = 0.0

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def open(self, timeout: float = 2.0) -> None:
        self.close()
        self.token = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        self.sock.sendto(_dumps({"id": 1, "method": "udp_sess_new", "params": []}),
                         (self.ip, UDP_PORT))
        data, _ = self.sock.recvfrom(4096)
        reply = json.loads(data.decode())
        params = reply.get("params")
        if isinstance(params, dict) and params.get("token"):
            self.token = params["token"]
        elif isinstance(reply.get("result"), list) and reply["result"]:
            self.token = reply["result"][0]
        if not self.token:
            raise RuntimeError(f"no token in UDP session reply: {reply}")
        self.sock.settimeout(0.0)  # non-blocking from now on
        self.last_alive = time.monotonic()

    def send(self, method: str, params: list) -> None:
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params, "token": self.token}
        trace(f"UDP {method} {params}")
        self.sock.sendto(_dumps(msg), (self.ip, UDP_PORT))

    def keepalive(self) -> None:
        self.send("udp_sess_keep_alive", ["keeplive_interval", "10"])

    def drain(self) -> None:
        """Read pending datagrams; any reply from the lamp counts as proof of life."""
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (BlockingIOError, socket.timeout):
                return
            except ConnectionResetError:
                return  # Windows: ICMP port unreachable from an earlier send
            text = re.sub(r'"token"\s*:\s*"[^"]*"', '"token":"..."', data.decode(errors="replace").strip())
            trace(f"UDP reply {text}")
            self.last_alive = time.monotonic()


# ----------------------------------------------------------------------
# Connection manager
# ----------------------------------------------------------------------

class Connection:
    """Finds the lamp (by device id if known), keeps TCP + UDP clients for it."""

    def __init__(self, ip: Optional[str] = None, cache_path: Optional[str] = None):
        self.pinned_ip = ip            # --ip given: no rediscovery
        self.cache_path = cache_path
        self.device_id: Optional[str] = None
        self.ip: Optional[str] = ip
        self.info: dict = {}
        self.tcp: Optional[LampTCP] = None
        self.udp: Optional[LampUDP] = None
        self._load_cache()

    # --- cache -----------------------------------------------------------
    def _load_cache(self) -> None:
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        self.device_id = cache.get("id")
        if not self.ip:
            self.ip = cache.get("ip")  # fallback if multicast discovery fails

    def _save_cache(self) -> None:
        if not self.cache_path or not self.device_id:
            return
        try:
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump({"id": self.device_id, "ip": self.ip}, fh)
        except OSError:
            pass

    # --- resolving -------------------------------------------------------
    def resolve(self) -> str:
        if self.pinned_ip:
            self.ip = self.pinned_ip
            return self.ip
        picked = pick_device(discover(), self.device_id)
        if picked:
            self.device_id, self.info = picked
            self.ip = self.info["ip"]
            self._save_cache()
            return self.ip
        if self.ip:  # discovery failed, try the last known address
            return self.ip
        raise RuntimeError("No Yeelight lamp found. Check LAN Control, the subnet, "
                           "and that Python may use the private network in the firewall.")

    def open(self) -> None:
        ip = self.resolve()
        self.close()
        self.tcp = LampTCP(ip)
        self.udp = LampUDP(ip)
        self.udp.open()

    def rediscover(self) -> bool:
        """Look for the lamp again (new DHCP address). True if the address changed."""
        if self.pinned_ip:
            return False
        old = self.ip
        picked = pick_device(discover(), self.device_id)
        if not picked:
            return False
        self.device_id, self.info = picked
        self.ip = self.info["ip"]
        self._save_cache()
        return self.ip != old

    def reopen_clients(self) -> None:
        self.close()
        self.tcp = LampTCP(self.ip)
        self.udp = LampUDP(self.ip)
        self.udp.open()

    def close(self) -> None:
        if self.tcp:
            self.tcp.close()
        if self.udp:
            self.udp.close()
