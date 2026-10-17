"""
Automatic qBittorrent configuration through its Web UI API.

When DualLink turns on, we remember the user's current proxy/protocol settings, point qBittorrent
at the DualLink proxy, and reconnect running downloads so they go through it. When DualLink turns
off, the original settings are put back. Works with qBittorrent 4.4+ and 5.x.
"""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

# Settings DualLink changes (and later restores). Only keys the running qBittorrent knows are touched.
MANAGED_KEYS = (
    "proxy_type", "proxy_ip", "proxy_port", "proxy_auth_enabled",
    "proxy_peer_connections", "proxy_bittorrent", "proxy_torrents_only",
    "proxy_misc", "proxy_rss", "proxy_hostname_lookup",
    "bittorrent_protocol", "max_connec", "max_connec_per_torrent",
)


class QbtError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind  # "unreachable" | "auth" | "error"


class QbtWebUI:
    def __init__(self, url="http://127.0.0.1:8080", username="", password="", timeout=5):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._logged_in = False

    # ---- low level -------------------------------------------------------- #
    def _req(self, path, fields=None):
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Referer": self.url, "Origin": self.url})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise QbtError("auth", "qBittorrent refused access - check the Web UI username/password, "
                                       "or tick 'Bypass authentication for clients on localhost'.")
            raise QbtError("error", f"qBittorrent returned HTTP {e.code} for {path}")
        except (urllib.error.URLError, OSError):
            raise QbtError("unreachable", "Can't reach qBittorrent's Web UI. Is qBittorrent running "
                                          "with the Web UI turned on?")

    def _call(self, path, fields=None):
        if not self._logged_in:
            self.login()
        try:
            return self._req(path, fields)
        except QbtError as e:
            if e.kind == "auth" and self.username:
                self._logged_in = False
                self.login()
                return self._req(path, fields)
            raise

    # ---- API ---------------------------------------------------------------- #
    def login(self):
        if self.username:
            res = self._req("/api/v2/auth/login", {"username": self.username, "password": self.password})
            if res.strip() != b"Ok.":
                raise QbtError("auth", "qBittorrent rejected the Web UI username/password.")
        self._logged_in = True

    def version(self):
        return self._call("/api/v2/app/version").decode().strip()

    def preferences(self):
        return json.loads(self._call("/api/v2/app/preferences"))

    def set_preferences(self, prefs):
        self._call("/api/v2/app/setPreferences", {"json": json.dumps(prefs)})

    def downloading_hashes(self):
        items = json.loads(self._call("/api/v2/torrents/info?filter=downloading"))
        return [t["hash"] for t in items]

    def reconnect(self, hashes):
        """Stop + start torrents so their peer connections are re-made with the new settings."""
        if not hashes:
            return
        h = {"hashes": "|".join(hashes)}
        try:                                   # qBittorrent 5.x
            self._call("/api/v2/torrents/stop", h)
            self._call("/api/v2/torrents/start", h)
        except QbtError as e:
            if e.kind != "error":
                raise
            self._call("/api/v2/torrents/pause", h)  # qBittorrent 4.x
            self._call("/api/v2/torrents/resume", h)

    # ---- DualLink-specific ------------------------------------------------- #
    def apply_dual_link(self, port):
        """Point qBittorrent at the proxy. Returns the original values so they can be restored."""
        cur = self.preferences()
        saved = {k: cur[k] for k in MANAGED_KEYS if k in cur}
        new = {
            "proxy_ip": "127.0.0.1",
            "proxy_port": int(port),
            "proxy_auth_enabled": False,
            "proxy_peer_connections": True,
            "proxy_bittorrent": True,
            "proxy_torrents_only": False,
            "proxy_misc": False,
            "proxy_rss": False,
            "proxy_hostname_lookup": True,
            "bittorrent_protocol": 1,  # TCP only - uTP can't go through a SOCKS proxy
            "max_connec": max(int(cur.get("max_connec", 0) or 0), 500),
            "max_connec_per_torrent": max(int(cur.get("max_connec_per_torrent", 0) or 0), 200),
        }
        # qBittorrent 5 uses a name for the proxy type, 4.x uses a number (2 = SOCKS5)
        new["proxy_type"] = "SOCKS5" if isinstance(cur.get("proxy_type"), str) else 2
        if "max_connec" in cur and int(cur["max_connec"]) < 0:  # -1 = unlimited: leave it
            new.pop("max_connec")
        if "max_connec_per_torrent" in cur and int(cur["max_connec_per_torrent"]) < 0:
            new.pop("max_connec_per_torrent")
        new = {k: v for k, v in new.items() if k in cur or k == "proxy_type"}
        self.set_preferences(new)
        return saved

    def restore(self, saved):
        if saved:
            self.set_preferences(saved)
