"""
App logic: settings, turning DualLink on/off, live statistics, qBittorrent auto-setup.
Has no GUI dependencies, so it can be tested on any OS.
"""
import json
import os
import pathlib
import sys
import threading
import time
from collections import deque

from .engine import BalancingProxy, Link, list_adapters, looks_like_hotspot
from .qbt import QbtError, QbtWebUI

HISTORY = 90  # seconds of speed history kept for the graph

# Phone tethering address ranges (iPhone, Android USB/Wi-Fi). Used to find the hotspot again if it
# comes back as a differently-named adapter (e.g. a different USB port).
PHONE_RANGES = ("172.20.10.", "192.168.42.", "192.168.43.")

DEFAULTS = {
    "eth_name": "", "hot_name": "",      # adapters are remembered by name - their IP may change
    "eth_ip": "", "hot_ip": "",          # last known addresses (display + migration from 1.0/1.1)
    "auto": True, "recycle": True,
    "share_eth": 1.0, "share_hot": 1.0,
    "quota_mb": 0,
    "port": 1080,
    "qbt_auto": True, "qbt_url": "http://127.0.0.1:8080", "qbt_user": "", "qbt_pass": "",
    "reconnect": True,
    "tray": True, "autostart": False,
    "qbt_saved": None,     # original qBittorrent settings while DualLink is on (crash recovery)
    "tray_hint_shown": False,
    "fdm": False,          # Free Download Manager stays pointed at the proxy -> keep it up in pass-through when off
}


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    p = pathlib.Path(base) / "DualLink"
    p.mkdir(parents=True, exist_ok=True)
    return p


def pretty_adapter(name):
    for prefix in ("Ethernet adapter ", "Wireless LAN adapter ", "Unknown adapter ", "PPP adapter "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


VIRTUAL_HINTS = ("vethernet", "virtual", "vmware", "virtualbox", "hyper-v", "loopback", "bluetooth",
                 "tap-", "tailscale", "zerotier", "wireguard", "openvpn", "vpn")


def is_virtual(name):
    low = name.lower()
    return any(h in low for h in VIRTUAL_HINTS)


class SettingsError(ValueError):
    """A settings change that was refused, with a message meant for the user."""


class Controller:
    def __init__(self, cfg_path=None, adapter_lister=list_adapters):
        self.cfg_path = pathlib.Path(cfg_path) if cfg_path else config_dir() / "config.json"
        self.cfg = dict(DEFAULTS)
        self._load()
        self._list_adapters = adapter_lister
        self.links = [Link("Ethernet"), Link("Hotspot")]
        self.proxy = BalancingProxy(self.links)
        self.running = False
        self.qbt_state = {"state": "off", "message": ""}
        self.notify = lambda title, msg: None      # set by the GUI (tray notification)
        self.on_change = lambda: None              # set by the GUI (refresh tray menu)
        self._lock = threading.RLock()
        self._hist = [deque([0.0] * HISTORY, maxlen=HISTORY) for _ in self.links]
        self._speed = [0.0, 0.0]
        self._last = [(0, time.time()), (0, time.time())]
        self._quota_warned = False
        self.watch_every = 3.0                     # seconds between adapter checks while running
        self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler.start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        self.standby_error = None
        self.ensure_standby()
        if self.cfg.get("qbt_saved"):   # DualLink was not shut down cleanly last time
            threading.Thread(target=self._restore_leftover, daemon=True).start()

    # ---- settings --------------------------------------------------------- #
    def _load(self):
        try:
            self.cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
        except Exception:
            pass

    def save(self):
        try:
            self.cfg_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except Exception:
            pass

    def public_settings(self):
        return {k: v for k, v in self.cfg.items() if k not in ("qbt_saved",)}

    def update_settings(self, new):
        allowed = set(DEFAULTS) - {"qbt_saved"}
        new = dict(new or {})
        eth = new.get("eth_name", self.cfg["eth_name"])
        hot = new.get("hot_name", self.cfg["hot_name"])
        if eth and eth == hot:
            raise SettingsError("Ethernet and hotspot must be two different connections.")
        with self._lock:
            for k, v in (new or {}).items():
                if k in allowed:
                    self.cfg[k] = v
            self.cfg["quota_mb"] = max(0.0, float(self.cfg.get("quota_mb") or 0))
            port = self.cfg.get("port")
            self.cfg["port"] = int(port) if port not in (None, "") else 1080
            for k in ("share_eth", "share_hot"):
                self.cfg[k] = max(0.1, float(self.cfg.get(k) or 1))
            self.save()
            self._apply_live_settings()
        if "fdm" in new or "port" in new:
            self.ensure_standby()
        if self.running and ("eth_name" in new or "hot_name" in new):
            self.check_adapters()          # switch connections right away
        return self.public_settings()

    def _apply_live_settings(self):
        self.proxy.auto = bool(self.cfg["auto"])
        self.proxy.recycle = bool(self.cfg["recycle"])
        if not self.proxy.auto:
            self.links[0].weight = float(self.cfg["share_eth"])
            self.links[1].weight = float(self.cfg["share_hot"])
        self.links[1].quota_bytes = int(float(self.cfg["quota_mb"]) * 1024 * 1024)
        if not self.links[1].over_quota():
            self._quota_warned = False

    # ---- adapters ---------------------------------------------------------- #
    def _scan(self):
        items, seen = [], set()
        for n, ip in self._list_adapters():
            name = pretty_adapter(n)
            if name in seen:          # adapter with several IPv4 addresses: keep the first
                continue
            seen.add(name)
            items.append({"name": name, "ip": ip, "hotspot": looks_like_hotspot(n, ip)})
        return items

    @staticmethod
    def _ip_of(items, name):
        return next((a["ip"] for a in items if a["name"] == name), "") if name else ""

    def adapters(self):
        """Adapters for the Settings list. Fills in sensible defaults on first run."""
        items = self._scan()
        real = [a for a in items if not is_virtual(a["name"])] or items
        with self._lock:
            # migrate settings from 1.0/1.1, which stored IPs only
            for key in ("eth", "hot"):
                if not self.cfg[f"{key}_name"] and self.cfg[f"{key}_ip"]:
                    self.cfg[f"{key}_name"] = next((a["name"] for a in items if a["ip"] == self.cfg[f"{key}_ip"]), "")
            eth, hot = self.cfg["eth_name"], self.cfg["hot_name"]
            # first run: guess. A saved choice is never replaced just because it's unplugged right now.
            if not hot:
                hot = next((a["name"] for a in real if a["hotspot"] and a["name"] != eth), "")
            if not eth:
                eth = next((a["name"] for a in real if a["name"] != hot and not a["hotspot"]), "") or \
                      next((a["name"] for a in real if a["name"] != hot), "")
            if not hot:
                hot = next((a["name"] for a in real if a["name"] != eth), "")
            self.cfg["eth_name"], self.cfg["hot_name"] = eth, hot
            for key, name in (("eth", eth), ("hot", hot)):
                ip = self._ip_of(items, name)
                if ip:
                    self.cfg[f"{key}_ip"] = ip
        out = [dict(a, present=True) for a in items]
        for name in (eth, hot):          # keep an unplugged choice visible in the list
            if name and not any(a["name"] == name for a in items):
                out.append({"name": name, "ip": "", "hotspot": False, "present": False})
        return out

    # ---- qBittorrent ------------------------------------------------------- #
    def _qbt(self):
        return QbtWebUI(self.cfg["qbt_url"], self.cfg["qbt_user"], self.cfg["qbt_pass"])

    def test_qbt(self):
        try:
            v = self._qbt().version()
            return {"ok": True, "message": f"Connected to qBittorrent {v}"}
        except QbtError as e:
            return {"ok": False, "kind": e.kind, "message": str(e)}

    def _restore_leftover(self):
        for _ in range(30):  # qBittorrent may start after us: keep trying for a minute
            if self.running or not self.cfg.get("qbt_saved"):
                return
            try:
                q = self._qbt()
                q.restore(self.cfg["qbt_saved"])
                self.cfg["qbt_saved"] = None
                self.save()
                return
            except QbtError:
                time.sleep(2)

    # ---- power ---------------------------------------------------------- #
    def power_on(self):
        with self._lock:
            if self.running:
                return {"ok": True}
            eth, hot = self.cfg["eth_name"], self.cfg["hot_name"]
            if not eth or not hot:
                return {"ok": False, "message": "Pick both an Ethernet and a hotspot connection in Settings."}
            if eth == hot:
                return {"ok": False, "message": "Ethernet and hotspot must be two different connections."}
            items = self._scan()
            ips = [self._ip_of(items, eth), self._ip_of(items, hot)]
            if not any(ips):
                return {"ok": False, "message": "Neither connection is available right now. "
                                                "Check that Ethernet or your phone is connected."}
            note = None
            if not ips[0]:
                note = f"{eth} isn't connected - DualLink is using the hotspot and will add Ethernet when it's back."
            elif not ips[1]:
                note = f"{hot} isn't connected yet - DualLink will add the hotspot automatically when it appears."
            for l, ip in zip(self.links, ips):
                l.ip = ip
                l.down = l.up = l.failures = l.udp_bytes = 0
                l.rate = l.per_conn = 0.0
                l._last_down = 0
                l.measured_at = -1e9
                l.weight = 1.0
                l.enabled = True
            self._apply_live_settings()
            self.proxy.moved = 0
            self._quota_warned = False
            self._last = [(0, time.time()), (0, time.time())]
            try:
                if self.proxy_up() and self.proxy.port != self.cfg["port"] and self.cfg["port"]:
                    self.proxy.stop()                 # standby proxy on an old port
                if self.proxy_up():
                    self.proxy.close_link(self.proxy.direct)   # FDM reconnects its pieces over both links
                else:
                    self.proxy.start(self.cfg["port"])
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": str(e)}
            self.proxy.passthrough = False
            self.running = True

        if note:
            self.notify("DualLink is on", note)
        if self.cfg["qbt_auto"]:
            self._qbt_apply()
        else:
            self.qbt_state = {"state": "disabled", "message": ""}
        self.on_change()
        return {"ok": True, "message": note} if note else {"ok": True}

    # ---- pass-through standby (for apps that stay pointed at the proxy) ----- #
    def proxy_up(self):
        return bool(self.proxy.loop and self.proxy.loop.is_running())

    def ensure_standby(self):
        """While off: keep the proxy up in pass-through mode if FDM is used, otherwise keep it down."""
        with self._lock:
            if self.running:
                return
            want = bool(self.cfg.get("fdm"))
            if self.proxy_up() and (not want or (self.cfg["port"] and self.proxy.port != self.cfg["port"])):
                self.proxy.stop()
            if want and not self.proxy_up():
                self.proxy.passthrough = True
                try:
                    self.proxy.start(self.cfg["port"])
                    self.standby_error = None
                except Exception as e:  # noqa: BLE001
                    self.standby_error = str(e)

    def _qbt_apply(self):
        try:
            q = self._qbt()
            saved = q.apply_dual_link(self.proxy.port)
            if not self.cfg.get("qbt_saved"):   # keep the true originals if we crashed while on
                self.cfg["qbt_saved"] = saved
                self.save()
            if self.cfg["reconnect"]:
                q.reconnect(q.downloading_hashes())
            self.qbt_state = {"state": "connected", "message": "qBittorrent is set up and using DualLink."}
        except QbtError as e:
            self.qbt_state = {"state": e.kind, "message": str(e)}

    def power_off(self):
        with self._lock:
            if not self.running:
                return {"ok": True}
            self.running = False
        if self.cfg.get("qbt_saved"):
            try:
                q = self._qbt()
                q.restore(self.cfg["qbt_saved"])
                self.cfg["qbt_saved"] = None
                self.save()
                if self.cfg["reconnect"]:
                    q.reconnect(q.downloading_hashes())
            except QbtError:
                pass  # will be retried at next start
        if self.cfg.get("fdm"):
            self.proxy.passthrough = True      # Free Download Manager keeps working over the normal route
        else:
            self.proxy.stop()
        self.qbt_state = {"state": "off", "message": ""}
        self.on_change()
        return {"ok": True}

    def retry_qbt(self):
        if self.running and self.cfg["qbt_auto"]:
            self._qbt_apply()
        return self.qbt_state

    # ---- following adapters while running ------------------------------------ #
    def _watch_loop(self):
        last = 0.0
        while True:
            time.sleep(0.1)
            if self.running and time.monotonic() - last >= self.watch_every:
                last = time.monotonic()
                try:
                    self.check_adapters()
                except Exception:
                    pass

    def check_adapters(self):
        """Follow IP changes, unplugging and re-plugging of both connections."""
        items = self._scan()
        changed = False
        for key, link, label in (("eth", self.links[0], "Ethernet"), ("hot", self.links[1], "Hotspot")):
            name = self.cfg[f"{key}_name"]
            ip = self._ip_of(items, name)
            if not ip and key == "hot":
                # the phone may come back as a differently-named adapter (another USB port, Wi-Fi instead of USB)
                other = self.cfg["eth_name"]
                cand = next((a for a in items if a["ip"].startswith(PHONE_RANGES) and a["name"] != other), None)
                if cand:
                    self.cfg["hot_name"], ip = cand["name"], cand["ip"]
                    changed = True
            if ip == link.ip or not self.running:
                continue
            old, link.ip = link.ip, ip
            if old:
                self.proxy.close_link(link)          # its connections are dead or on the old address
            if not ip:
                self.notify(f"{label} disconnected",
                            f"Downloads continue over the other connection. DualLink will add {label.lower()} "
                            f"back automatically when it reconnects.")
            elif not old:
                link.measured_at = -1e9              # treat as unknown again: fair share until measured
                self.notify(f"{label} connected", f"DualLink is using {self.cfg[f'{key}_name']} again ({ip}).")
            else:
                link.measured_at = -1e9
                self.notify(f"{label} got a new address", f"DualLink switched to {ip} automatically.")
            if ip:
                self.cfg[f"{key}_ip"] = ip
            changed = True
        if changed:
            self.save()
            self.on_change()

    # ---- stats -------------------------------------------------------------- #
    def _sample_loop(self):
        while True:
            time.sleep(1.0)
            now = time.time()
            for i, l in enumerate(self.links):
                prev, t0 = self._last[i]
                sp = max(0.0, (l.down - prev) / max(now - t0, 0.001)) if self.running else 0.0
                self._last[i] = (l.down, now)
                self._speed[i] = sp
                self._hist[i].append(sp)
            hot = self.links[1]
            if self.running and hot.over_quota() and not self._quota_warned:
                self._quota_warned = True
                self.notify("Hotspot data limit reached",
                            "DualLink stopped using the hotspot. Downloads continue over Ethernet.")

    def status(self):
        usable = [l for l in self.links if l.usable()]
        wsum = sum(l.weight for l in usable) or 1.0
        out = []
        for i, l in enumerate(self.links):
            out.append({
                "name": l.name, "ip": l.ip, "connections": l.active,
                "adapter": self.cfg["eth_name" if i == 0 else "hot_name"],
                "present": bool(l.ip) or not self.running,
                "share": round(100 * l.weight / wsum) if (self.running and l.usable()) else 0,
                "speed": self._speed[i], "down": l.down, "up": l.up,
                "quota": l.quota_bytes, "over_quota": l.over_quota(),
                "history": list(self._hist[i]),
            })
        return {
            "running": self.running,
            "combined": sum(self._speed),
            "links": out,
            "moved": self.proxy.moved,
            "dht": self.running and self.proxy.udp_assocs > 0,
            "port": self.proxy.port if self.running else self.cfg["port"],
            "qbt": self.qbt_state,
            "auto": bool(self.cfg["auto"]),
            "fdm": bool(self.cfg.get("fdm")),
            "qbt_auto": bool(self.cfg.get("qbt_auto")),
            "standby": (not self.running) and self.proxy_up(),
            "standby_error": self.standby_error,
        }

    def shutdown(self):
        self.power_off()
        self.proxy.stop()      # quitting the app: nothing left to pass through


# ---- Start with Windows ---------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else pathlib.Path(sys.executable)
    main = pathlib.Path(__file__).resolve().parent.parent / "main.py"
    return f'"{exe}" "{main}" --minimized'


def set_autostart(enabled):
    if os.name != "nt":
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, "DualLink", 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, "DualLink")
            except FileNotFoundError:
                pass
    return True
