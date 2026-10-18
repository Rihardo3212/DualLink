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

DEFAULTS = {
    "eth_ip": "", "hot_ip": "",
    "auto": True, "recycle": True,
    "share_eth": 1.0, "share_hot": 1.0,
    "quota_mb": 0,
    "port": 1080,
    "qbt_auto": True, "qbt_url": "http://127.0.0.1:8080", "qbt_user": "", "qbt_pass": "",
    "reconnect": True,
    "tray": True, "autostart": False,
    "qbt_saved": None,     # original qBittorrent settings while DualLink is on (crash recovery)
    "tray_hint_shown": False,
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
        self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler.start()
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
    def adapters(self):
        items = [{"name": pretty_adapter(n), "ip": ip, "hotspot": looks_like_hotspot(n, ip)}
                 for n, ip in self._list_adapters()]
        ips = [a["ip"] for a in items]
        real = [a for a in items if not is_virtual(a["name"])] or items
        with self._lock:
            hot, eth = self.cfg["hot_ip"], self.cfg["eth_ip"]
            if hot not in ips:
                hot = next((a["ip"] for a in real if a["hotspot"] and a["ip"] != eth), "")
            if eth not in ips or eth == hot:
                eth = next((a["ip"] for a in real if a["ip"] != hot and not a["hotspot"]), "") or \
                      next((a["ip"] for a in real if a["ip"] != hot), "")
            if not hot:   # nothing looks like a phone: use the other real connection
                hot = next((a["ip"] for a in real if a["ip"] != eth), "")
            self.cfg["hot_ip"], self.cfg["eth_ip"] = hot, eth
        return items

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
            known = {ip for _n, ip in self._list_adapters()}
            eth, hot = self.cfg["eth_ip"], self.cfg["hot_ip"]
            if not eth or not hot:
                return {"ok": False, "message": "Pick both an Ethernet and a hotspot connection in Settings."}
            if eth == hot:
                return {"ok": False, "message": "Ethernet and hotspot must be two different connections."}
            missing = [n for n, ip in (("Ethernet", eth), ("Hotspot", hot)) if ip not in known]
            if missing:
                return {"ok": False, "message": f"{' and '.join(missing)} connection is not available right now. "
                                                "Check it's connected, then pick it again in Settings."}
            self.links[0].ip, self.links[1].ip = eth, hot
            for l in self.links:
                l.down = l.up = l.failures = 0
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
                self.proxy.start(self.cfg["port"])
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": str(e)}
            self.running = True

        if self.cfg["qbt_auto"]:
            self._qbt_apply()
        else:
            self.qbt_state = {"state": "manual", "message": "Automatic qBittorrent setup is off."}
        self.on_change()
        return {"ok": True}

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
        self.proxy.stop()
        self.qbt_state = {"state": "off", "message": ""}
        self.on_change()
        return {"ok": True}

    def retry_qbt(self):
        if self.running and self.cfg["qbt_auto"]:
            self._qbt_apply()
        return self.qbt_state

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
        }

    def shutdown(self):
        self.power_off()


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
