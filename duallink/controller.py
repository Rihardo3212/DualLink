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

from . import __version__
from .engine import BalancingProxy, Link, list_adapters, looks_like_hotspot
from .paths import config_dir
from .qbt import QbtError, QbtWebUI

HISTORY = 90  # seconds of speed history kept for the graph

# Phone tethering address ranges (iPhone, Android USB/Wi-Fi). Used to find the hotspot again if it
# comes back as a differently-named adapter (e.g. a different USB port).
PHONE_RANGES = ("172.20.10.", "192.168.42.", "192.168.43.")

DEFAULTS = {
    "eth_name": "", "hot_name": "",      # adapters are remembered by name - their IP may change
    "extra": [],                         # up to MAX_EXTRA more adapters (Wi-Fi, a second phone, ...)
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
    # Apps set up by hand (they stay pointed at the proxy) -> keep it up in pass-through mode while DualLink is off
    "fdm": False, "jd": False, "idm": False, "firefox": False, "other": False,
    "auto_on_hotspot": True,   # turn on when the hotspot connects
    "auto_on_start": False,    # turn on when DualLink starts
    "check_updates": True,
    "learned": {},             # adapter name -> {"per_conn": bytes/s per connection, "ts": epoch} (internal)
    "speedtest": None,         # last speed test result (internal)
    "seen_apps": {},           # app key -> last time it was seen connected to DualLink (internal)
    "on_after_update": False,  # DualLink was on when an in-app update started: turn on again after it (internal)
}


MANUAL_APPS = ("fdm", "jd", "idm", "firefox", "other")
INTERNAL_KEYS = ("qbt_saved", "learned", "speedtest", "seen_apps", "on_after_update")
MAX_EXTRA = 2
APP_KEYS = ("qbt",) + MANUAL_APPS
APP_PROCESSES = {           # program names (lower case) -> app
    "qbt": ("qbittorrent.exe",),
    "fdm": ("fdm.exe",),
    "jd": ("jdownloader2.exe", "jdownloader.exe", "javaw.exe", "java.exe"),
    "idm": ("idman.exe",),
    "firefox": ("firefox.exe",),
}


def classify_clients(counts):
    """{program: connections} -> {app_key: {"connections": n, "programs": [...]}} (unknown programs -> "other")."""
    live = {k: {"connections": 0, "programs": []} for k in APP_KEYS}
    for name, c in counts.items():
        key = next((k for k, procs in APP_PROCESSES.items() if name.lower() in procs), "other")
        live[key]["connections"] += c
        if name not in live[key]["programs"]:
            live[key]["programs"].append(name)
    return live
LEARNED_MAX_AGE = 30 * 24 * 3600     # forget remembered speeds after 30 days


def fmt_rate(b):
    for unit in ("B/s", "KiB/s", "MiB/s"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B/s" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} GiB/s"


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
    def __init__(self, cfg_path=None, adapter_lister=list_adapters, ipv6_lister=None):
        self.cfg_path = pathlib.Path(cfg_path) if cfg_path else config_dir() / "config.json"
        self.cfg = dict(DEFAULTS)
        self._load()
        self._list_adapters = adapter_lister
        if ipv6_lister is None:
            from .diag import adapter_ipv6 as ipv6_lister
        self._list_ipv6 = ipv6_lister
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
        self._sync_links()
        self.app_live = {}                        # live app status (who is connected to DualLink right now)
        self.ui_visible = True                    # False while the window is hidden in the tray or minimised
        self.app_every = 5.0
        self.app_probe = None                     # override for tests; default: diag.proxy_clients
        threading.Thread(target=self._app_loop, daemon=True).start()
        self.watch_every = 3.0                     # seconds between adapter checks while running
        self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler.start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        self.standby_error = None
        self._hot_present = None                  # for "turn on when the hotspot connects"
        self._last_learn = time.monotonic()
        self.update = None                        # details of a newer release, when there is one
        self.update_state = {"phase": "idle", "done": 0, "total": 0, "message": ""}
        self.just_updated = False
        self.quit_app = lambda: None              # set by the GUI: close DualLink so the update can install
        from . import updates as _u
        self.install_kind_fn, self.update_download = _u.install_kind, _u.download
        self.update_installer, self.update_portable = _u.run_installer, _u.swap_portable
        self._update_notified = None
        self.speedtest_url = None                 # override for tests
        self.speedtest_seconds = 5.0
        self.speedtest_state = {"running": False, "phase": "", "result": self.cfg.get("speedtest")}
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
        return {k: v for k, v in self.cfg.items() if k not in INTERNAL_KEYS}

    def manual_apps_on(self):
        return any(self.cfg.get(k) for k in MANUAL_APPS)

    def update_settings(self, new):
        allowed = set(DEFAULTS) - set(INTERNAL_KEYS)
        new = dict(new or {})
        eth = new.get("eth_name", self.cfg["eth_name"])
        hot = new.get("hot_name", self.cfg["hot_name"])
        if eth and eth == hot:
            raise SettingsError("Ethernet and hotspot must be two different connections.")
        if "extra" in new:
            new["extra"] = [n for n in (new["extra"] or []) if isinstance(n, str) and n][:MAX_EXTRA]
        extra = new.get("extra", self.extra_names())
        names = [n for n in [eth, hot] + list(extra) if n]
        if len(names) != len(set(names)):
            raise SettingsError("Each connection can only be used once.")
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
        if "port" in new or any(k in new for k in MANUAL_APPS):
            self.ensure_standby()
        if "extra" in new:
            self._sync_links()
        if self.running and ("eth_name" in new or "hot_name" in new or "extra" in new):
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

    # ---- links: Ethernet, Hotspot, then up to MAX_EXTRA extra connections ---------- #
    def extra_names(self):
        return [n for n in (self.cfg.get("extra") or []) if n][:MAX_EXTRA]

    def link_names(self):
        return [self.cfg["eth_name"], self.cfg["hot_name"]] + self.extra_names()

    def link_labels(self):
        return ["Ethernet", "Hotspot"] + self.extra_names()

    def _sync_links(self):
        """Make self.links match the configured connections (the proxy shares this list, so edit in place)."""
        with self._lock:
            extras = self.extra_names()
            while len(self.links) > 2 + len(extras):
                gone = self.links.pop()
                self.proxy.close_link(gone)
                self._hist.pop(); self._speed.pop(); self._last.pop()
            while len(self.links) < 2 + len(extras):
                self.links.append(Link(""))
                self._hist.append(deque([0.0] * HISTORY, maxlen=HISTORY))
                self._speed.append(0.0)
                self._last.append((0, time.time()))
            for l, name in zip(self.links[2:], extras):
                if l.name != name:                # a different adapter now uses this slot
                    if l.ip:
                        self.proxy.close_link(l)
                    l.name, l.ip, l.ip6 = name, "", ""
                    l.down = l.up = l.udp_bytes = 0
                    l.rate = l.per_conn = 0.0
                    l._last_down = 0
                    l.measured_at = -1e9
                    l.weight = 1.0

    # ---- adapters ---------------------------------------------------------- #
    def _scan(self):
        try:
            v6 = self._list_ipv6() or {}
        except Exception:
            v6 = {}
        items, seen = [], set()
        for n, ip in self._list_adapters():
            name = pretty_adapter(n)
            if name in seen:          # adapter with several IPv4 addresses: keep the first
                continue
            seen.add(name)
            items.append({"name": name, "ip": ip, "ip6": v6.get(name, ""), "hotspot": looks_like_hotspot(n, ip)})
        return items

    @staticmethod
    def _ip6_of(items, name):
        return next((a["ip6"] for a in items if a["name"] == name), "") if name else ""

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
        for name in [eth, hot] + self.extra_names():   # keep an unplugged choice visible in the list
            if name and not any(a["name"] == name for a in items):
                out.append({"name": name, "ip": "", "ip6": "", "hotspot": False, "present": False})
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
            names = self.link_names()
            ips = [self._ip_of(items, n) for n in names]
            if not any(ips):
                return {"ok": False, "message": "Neither connection is available right now. "
                                                "Check that Ethernet or your phone is connected."}
            note = None
            if not ips[0]:
                note = f"{eth} isn't connected - DualLink is using the hotspot and will add Ethernet when it's back."
            elif not ips[1]:
                note = f"{hot} isn't connected yet - DualLink will add the hotspot automatically when it appears."
            for l, ip, name in zip(self.links, ips, names):
                l.ip = ip
                l.ip6 = self._ip6_of(items, name)
                l.down = l.up = l.failures = l.udp_bytes = 0
                l.rate = l.per_conn = 0.0
                l._last_down = 0
                l.measured_at = -1e9
                l.weight = 1.0
                l.enabled = True
            self._seed_speeds()
            self._apply_live_settings()
            self.proxy.moved = 0
            self.proxy.refused_ipv6 = self.proxy.failed = 0
            self._quota_warned = False
            self._last[:] = [(0, time.time()) for _ in self.links]
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

    # ---- live app status --------------------------------------------------------- #
    def check_apps(self):
        """Which apps are connected to DualLink right now; remembers when each was last seen working."""
        if not self.proxy_up():
            self.app_live = {}
            return
        probe = self.app_probe
        if probe is None:
            from .diag import proxy_clients as probe
        live = classify_clients(probe(self.proxy.port, os.getpid()) or {})
        self.app_live = live
        seen = dict(self.cfg.get("seen_apps") or {})
        newly = [k for k, v in live.items() if v["connections"] and k not in seen]
        for k, v in live.items():
            if v["connections"]:
                seen[k] = time.time()
        self.cfg["seen_apps"] = seen
        if newly or time.monotonic() - getattr(self, "_seen_saved", 0) > 300:
            self._seen_saved = time.monotonic()
            self.save()

    def _app_loop(self):
        last = 0.0
        while True:
            time.sleep(0.2)
            if time.monotonic() - last >= self.app_every:
                last = time.monotonic()
                try:
                    self.check_apps()
                except Exception:
                    pass

    # ---- remembered speeds ------------------------------------------------------ #
    def remember_speeds(self):
        """Store each link's measured speed per connection, so the next session starts with it."""
        now_m, changed = time.monotonic(), False
        learned = dict(self.cfg.get("learned") or {})
        for l, name in zip(self.links, self.link_names()):
            if name and l.per_conn > 0 and now_m - l.measured_at < 60:
                learned[name] = {"per_conn": l.per_conn, "ts": time.time()}
                changed = True
        if changed:
            self.cfg["learned"] = learned
            self.save()

    def _seed_values(self):
        """[eth, hot] speeds to start balancing with (remembered or from the last speed test), or None."""
        now, names = time.time(), self.link_names()
        learned = self.cfg.get("learned") or {}
        vals = [learned.get(n) for n in names]
        if all(v and now - v.get("ts", 0) < LEARNED_MAX_AGE and v.get("per_conn", 0) > 0 for v in vals):
            return [v["per_conn"] for v in vals]
        st = self.cfg.get("speedtest") or {}
        if now - st.get("ts", 0) < LEARNED_MAX_AGE:
            tested = {r.get("name"): r.get("bps", 0) for r in st.get("links", [])}
            vals = [tested.get(n, 0) for n in names]
            if all(v > 0 for v in vals):
                return vals
        return None

    def _seed_speeds(self):
        seeds = self._seed_values()
        if not seeds:
            return
        now_m, total = time.monotonic(), sum(seeds)
        for l, v in zip(self.links, seeds):
            l.per_conn = float(v)
            l.measured_at = now_m                 # counts as measured: no even-split warm-up needed
            share = v / total
            l.weight = min(max(share, self.proxy.min_share), 1 - self.proxy.min_share)

    # ---- speed test ------------------------------------------------------------ #
    def start_speed_test(self):
        if self.speedtest_state["running"]:
            return {"ok": True}
        items = self._scan()
        names, labels = self.link_names(), self.link_labels()
        ips = [self._ip_of(items, n) for n in names]
        if not any(ips):
            return {"ok": False, "message": "None of your connections is available right now."}
        self.speedtest_state = {"running": True, "phase": "", "result": None}
        threading.Thread(target=self._run_speed_test, args=(names, labels, ips), daemon=True).start()
        return {"ok": True}

    def _run_speed_test(self, names, labels, ips):
        from . import speedtest
        result = {"ts": time.time(), "links": []}
        for name, label, ip in zip(names, labels, ips):
            self.speedtest_state["phase"] = label
            if not ip:
                r = {"bps": 0, "error": f"{label} isn't connected."}
            else:
                kw = {"seconds": self.speedtest_seconds}
                if self.speedtest_url:
                    kw["url"] = self.speedtest_url
                try:
                    r = speedtest.measure(ip, **kw)
                except Exception as e:  # noqa: BLE001
                    r = {"bps": 0, "error": str(e)}
            result["links"].append(dict(r, name=name, label=label))
        result["eth"], result["hot"] = result["links"][0], result["links"][1]
        self.cfg["speedtest"] = result
        self.save()
        if self.running and all(time.monotonic() - l.measured_at > 60 for l in self.links):
            self._seed_speeds()                   # nothing measured yet this session: use the test right away
        self.speedtest_state = {"running": False, "phase": "", "result": result}

    # ---- updates ------------------------------------------------------------- #
    def check_updates_now(self, fetch=None):
        from . import updates
        if not updates.REPO and fetch is None:
            return {"ok": False, "message": "Update checks work in the version built by GitHub."}
        try:
            kw = {"fetch": fetch} if fetch else {}
            self.update = updates.check(repo=updates.REPO or "test/test", **kw)
        except Exception:
            return {"ok": False, "message": "Couldn't reach GitHub to check for updates."}
        if self.update and self._update_notified != self.update["version"]:
            self._update_notified = self.update["version"]
            self.notify(f"DualLink {self.update['version']} is available", "Open DualLink to download it.")
        return {"ok": True, "update": self.update}

    def start_update(self):
        """Download the new version, check it, switch off safely, install and restart."""
        if self.update_state["phase"] in ("downloading", "installing"):
            return {"ok": True}
        if not self.update:
            return {"ok": False, "message": "There's no update to install."}
        kind = self.install_kind_fn()
        if kind == "source":
            return {"ok": False, "message": "You're running DualLink from source - update with git pull instead."}
        asset = (self.update.get("assets") or {}).get("installer" if kind == "installed" else "portable")
        if not asset or not asset.get("url"):
            return {"ok": False, "message": "This release has no file for your kind of install - use the download page."}
        self.update_state = {"phase": "downloading", "done": 0, "total": asset.get("size", 0), "message": ""}
        threading.Thread(target=self._run_update, args=(kind, asset), daemon=True).start()
        return {"ok": True}

    def _run_update(self, kind, asset):
        try:
            def progress(done, total):
                self.update_state.update(done=done, total=total)
            path = self.update_download(asset, progress=progress)
            self.update_state.update(phase="installing")
            if self.running:
                self.cfg["on_after_update"] = True    # the new version turns on again (see startup)
                self.save()
            self.power_off()                      # gives qBittorrent its own settings back first
            if kind == "installed":
                self.update_installer(path)       # the installer starts the new version when done
            else:
                self.update_portable(path)        # swaps DualLink.exe and starts the new one
            self.quit_app()
        except Exception as e:  # noqa: BLE001
            was_on = bool(self.cfg.get("on_after_update"))
            if was_on:
                self.cfg["on_after_update"] = False
                self.save()
            self.update_state = {"phase": "error", "done": 0, "total": 0, "message": str(e)}
            if was_on and not self.running:
                try:
                    self.power_on()               # the update didn't happen: back to how it was
                except Exception:  # noqa: BLE001
                    pass

    def start_update_checks(self, first_delay=10, every=12 * 3600):
        def loop():
            time.sleep(first_delay)
            while True:
                if self.cfg.get("check_updates"):
                    self.check_updates_now()
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()

    # ---- turning on automatically ------------------------------------------------ #
    def startup(self):
        """Called once when the app starts."""
        resume = bool(self.cfg.get("on_after_update"))
        if resume:
            self.cfg["on_after_update"] = False   # only for the start right after the update
            self.save()
        wanted = self.cfg.get("auto_on_start") or (resume and self.just_updated)
        if wanted and self.cfg["eth_name"] and self.cfg["hot_name"] and not self.running:
            return self.power_on()
        return {"ok": True}

    def check_auto_on(self):
        """While off: turn on when the hotspot appears (not if it was already there - the user turned us off)."""
        hot = self.cfg["hot_name"]
        if not (hot and self.cfg["eth_name"]):
            return
        items = self._scan()
        present = bool(self._ip_of(items, hot))
        if not present:      # the phone may come back as a differently-named adapter
            cand = next((a for a in items if a["ip"].startswith(PHONE_RANGES) and a["name"] != self.cfg["eth_name"]), None)
            if cand and self._hot_present is False:
                self.cfg["hot_name"] = cand["name"]
                self.save()
                present = True
        before, self._hot_present = self._hot_present, present
        if before is False and present and not self.running and self.cfg.get("auto_on_hotspot"):
            r = self.power_on()
            if r.get("ok"):
                self.notify("DualLink turned on", "Your hotspot connected, so DualLink is combining it with Ethernet.")

    # ---- pass-through standby (for apps that stay pointed at the proxy) ----- #
    def proxy_up(self):
        return bool(self.proxy.loop and self.proxy.loop.is_running())

    def ensure_standby(self):
        """While off: keep the proxy up in pass-through mode if FDM is used, otherwise keep it down."""
        with self._lock:
            if self.running:
                return
            want = self.manual_apps_on()
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
            self.remember_speeds()
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
        if self.manual_apps_on():
            self.proxy.passthrough = True      # apps set up by hand keep working over the normal route
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
            if time.monotonic() - last >= self.watch_every:
                last = time.monotonic()
                try:
                    if self.running:
                        self.check_adapters()
                    elif self.cfg.get("auto_on_hotspot"):
                        self.check_auto_on()
                except Exception:
                    pass

    def check_adapters(self):
        """Follow IP changes, unplugging and re-plugging of both connections."""
        items = self._scan()
        changed = False
        names, labels = self.link_names(), self.link_labels()
        for i, (link, name, label) in enumerate(zip(list(self.links), names, labels)):
            key = ("eth", "hot")[i] if i < 2 else None
            ip = self._ip_of(items, name)
            link.ip6 = self._ip6_of(items, name) if ip else ""
            if not ip and key == "hot":
                # the phone may come back as a differently-named adapter (another USB port, Wi-Fi instead of USB)
                others = [n for j, n in enumerate(names) if j != 1]
                cand = next((a for a in items if a["ip"].startswith(PHONE_RANGES) and a["name"] not in others), None)
                if cand:
                    self.cfg["hot_name"], ip = cand["name"], cand["ip"]
                    link.ip6 = cand["ip6"]
                    changed = True
            if ip == link.ip or not self.running:
                continue
            old, link.ip = link.ip, ip
            if old:
                self.proxy.close_link(link)          # its connections are dead or on the old address
            shown = label if i < 2 else f"{label} connection"
            if not ip:
                self.notify(f"{shown} disconnected",
                            f"Downloads continue over the other connections. DualLink will add it back "
                            f"automatically when it reconnects.")
            elif not old:
                link.measured_at = -1e9              # treat as unknown again: fair share until measured
                self.notify(f"{shown} connected", f"DualLink is using {name} ({ip}).")
            else:
                link.measured_at = -1e9
                self.notify(f"{shown} got a new address", f"DualLink switched to {ip} automatically.")
            if ip and key:
                self.cfg[f"{key}_ip"] = ip
            changed = True
        self._hot_present = bool(self.links[1].ip)
        if changed:
            self.save()
            self.on_change()

    # ---- stats -------------------------------------------------------------- #
    def _sample_loop(self):
        while True:
            time.sleep(1.0)
            now = time.time()
            for i, l in enumerate(list(self.links)):
                if i >= len(self._last):
                    break
                prev, t0 = self._last[i]
                sp = max(0.0, (l.down - prev) / max(now - t0, 0.001)) if self.running else 0.0
                self._last[i] = (l.down, now)
                self._speed[i] = sp
                self._hist[i].append(sp)
            if self.running and time.monotonic() - self._last_learn > 60:
                self._last_learn = time.monotonic()
                self.remember_speeds()
            hot = self.links[1]
            if self.running and hot.over_quota() and not self._quota_warned:
                self._quota_warned = True
                self.notify("Hotspot data limit reached",
                            "DualLink stopped using the hotspot. Downloads continue over Ethernet.")

    def speeds(self):
        """[(label, bytes/s)] per connection, for the tray icon's tooltip."""
        return list(zip(self.link_labels(), list(self._speed)))

    def status(self, for_ui=False):
        if for_ui and not self.ui_visible:
            return {"hidden": True}          # window is in the tray or minimised: nothing to draw
        usable = [l for l in self.links if l.usable()]
        wsum = sum(l.weight for l in usable) or 1.0
        out = []
        names, labels = self.link_names(), self.link_labels()
        for i, l in enumerate(list(self.links)[:len(names)]):
            out.append({
                "name": labels[i], "ip": l.ip, "connections": l.active,
                "adapter": names[i], "kind": ("eth", "hot")[i] if i < 2 else "extra",
                "present": bool(l.ip) or not self.running,
                "share": round(100 * l.weight / wsum) if (self.running and l.usable()) else 0,
                "speed": self._speed[i], "down": l.down, "up": l.up,
                "quota": l.quota_bytes, "over_quota": l.over_quota(),
                "history": list(self._hist[i]),
            })
        return {
            "running": self.running,
            "combined": sum(self._speed),
            "app_live": self.app_live,
            "seen_apps": dict(self.cfg.get("seen_apps") or {}),
            "links": out,
            "moved": self.proxy.moved,
            "dht": self.running and self.proxy.udp_assocs > 0,
            "port": self.proxy.port if self.running else self.cfg["port"],
            "qbt": self.qbt_state,
            "auto": bool(self.cfg["auto"]),
            "fdm": bool(self.cfg.get("fdm")),
            "apps": {k: bool(self.cfg.get(k)) for k in MANUAL_APPS},
            "speedtest": self.speedtest_state,
            "update": self.update,
            "update_state": self.update_state,
            "just_updated": self.just_updated,
            "version": __version__,
            "qbt_auto": bool(self.cfg.get("qbt_auto")),
            "standby": (not self.running) and self.proxy_up(),
            "standby_error": self.standby_error,
        }

    # ---- traffic check --------------------------------------------------------- #
    def traffic_check(self, seconds=3.0):
        """Compare what Windows sees on each connection with what DualLink carries, and list programs."""
        from . import diag
        rx_fn = getattr(self, "diag_rx", diag.adapter_rx_bytes)
        conn_fn = getattr(self, "diag_conns", diag.connections_snapshot)
        links = self.links
        rx0, d0, t0 = rx_fn(), [l.down for l in links], time.monotonic()
        time.sleep(seconds)
        rx1, d1, t1 = rx_fn(), [l.down for l in links], time.monotonic()
        dt = max(t1 - t0, 0.001)
        port = self.proxy.port if self.proxy_up() else self.cfg["port"]
        conns = conn_fn(port, os.getpid())
        if not rx0 and not conns["through"] and not conns["direct"]:
            return {"ok": False, "message": "The traffic check needs Windows."}

        report_links, notes = [], []
        for i, (adapter, label) in enumerate(zip(self.link_names(), self.link_labels())):
            windows = None
            if adapter in rx0 and adapter in rx1 and rx1[adapter] >= rx0[adapter]:
                windows = (rx1[adapter] - rx0[adapter]) / dt
            mine = (d1[i] - d0[i]) / dt if (self.running and i < min(len(d0), len(d1))) else 0.0
            other = None if windows is None else max(0.0, windows - mine * 1.05)   # ~5% protocol overhead
            apps = sorted(conns["direct"].get(adapter, {}).items(), key=lambda kv: -kv[1])
            report_links.append({"label": label, "adapter": adapter, "windows": windows, "dualink": mine,
                                 "other": other, "direct_apps": [{"name": n, "connections": c} for n, c in apps]})
            if other is not None and other > max(256 * 1024, 0.15 * (windows or 0)):
                who = ", ".join(n for n, _ in apps[:4])
                notes.append(("warn", f"{fmt_rate(other)} on {label if i >= 2 else label.lower()} isn't going through DualLink"
                                      + (f". Programs connecting over it directly: {who}." if who else ".")))

        through = sorted(conns["through"].items(), key=lambda kv: -kv[1])
        direct_any = {}
        for per in conns["direct"].values():
            for n, c in per.items():
                direct_any[n] = direct_any.get(n, 0) + c
        for name, c in through:
            if direct_any.get(name):
                notes.append(("warn", f"{name} sends {c} connection{'s' if c != 1 else ''} through DualLink but also "
                                      f"connects directly {direct_any[name]} time{'s' if direct_any[name] != 1 else ''}, "
                                      f"so part of its download isn't split."))
        if self.proxy.refused_ipv6:
            n = self.proxy.refused_ipv6
            notes.append(("warn", f"DualLink couldn't make {n} IPv6 connection{'s' if n != 1 else ''} because neither of "
                                  f"your connections has an IPv6 address right now. Apps often connect directly instead, "
                                  f"so that traffic isn't split."))
        if not self.running:
            notes.insert(0, ("info", "DualLink is off, so nothing is being split. Turn it on and run the check while a "
                                     "download is going."))
        elif not through:
            notes.insert(0, ("warn", f"No program is connected through DualLink right now. Check your download app's "
                                     f"proxy setting (SOCKS5, 127.0.0.1, port {port})."))
        if self.running and not any(k == "warn" for k, _ in notes):
            notes.append(("good", "Everything matches: your downloads are going through DualLink."))
        return {"ok": True, "seconds": round(dt, 1), "links": report_links,
                "through": [{"name": n, "connections": c} for n, c in through],
                "refused_ipv6": self.proxy.refused_ipv6, "failed": self.proxy.failed,
                "notes": [{"kind": k, "text": t} for k, t in notes]}

    def shutdown(self):
        self.power_off()
        self.proxy.stop()      # quitting the app: nothing left to pass through


# ---- Uninstall clean-up ------------------------------------------------------ #
def cleanup(cfg_path=None):
    """Run by the uninstaller: give qBittorrent its own settings back and remove 'Start with Windows'."""
    path = pathlib.Path(cfg_path) if cfg_path else config_dir() / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    if cfg.get("qbt_saved"):
        try:
            QbtWebUI(cfg.get("qbt_url", DEFAULTS["qbt_url"]), cfg.get("qbt_user", ""), cfg.get("qbt_pass", "")).restore(cfg["qbt_saved"])
            cfg["qbt_saved"] = None
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass
    try:
        set_autostart(False)
    except Exception:
        pass


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
