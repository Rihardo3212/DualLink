"""Test helpers: SOCKS5 client, test servers, and a fake qBittorrent Web UI."""
import http.server
import json
import socket
import struct
import threading
import time
import urllib.parse


def socks_connect(proxy_port, host, port):
    c = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    c.sendall(b"\x05\x01\x00")
    assert c.recv(2) == b"\x05\x00"
    c.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack(">H", port))
    reply = b""
    while len(reply) < 10:
        reply += c.recv(10 - len(reply))
    return c, reply[1]


class PeerServer:
    """Accepts connections, records the client's source IP; behaviour depends on mode/source."""

    def __init__(self, mode="burst", rates=None):
        self.mode, self.rates, self.sources = mode, rates or {}, []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(200)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, a = self.sock.accept()
            except OSError:
                return
            self.sources.append(a[0])
            threading.Thread(target=self._serve, args=(c, a[0]), daemon=True).start()

    def _serve(self, c, src):
        try:
            if self.mode == "burst":
                c.sendall(b"x" * 200_000)
                time.sleep(1.0)
            elif self.mode == "idle":
                while c.recv(100):
                    pass
            elif self.mode == "stream":
                rate = self.rates.get(src, 50_000)
                while True:
                    c.sendall(b"x" * (rate // 10))
                    time.sleep(0.1)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.sock.close()


def reconnecting_client(proxy_port, target_port, stop):
    """Behaves like qBittorrent: when a peer connection drops, it connects again."""
    while not stop.is_set():
        try:
            c, _ = socks_connect(proxy_port, "127.0.0.1", target_port)
            c.settimeout(0.5)
            while not stop.is_set():
                try:
                    if not c.recv(65536):
                        break
                except socket.timeout:
                    continue
            c.close()
        except OSError:
            pass
        time.sleep(0.2)


class FakeQbt:
    """Minimal qBittorrent Web API: preferences, login, torrents info/stop/start (v5) or pause/resume (v4)."""

    def __init__(self, v5=True, username="", password=""):
        self.v5, self.username, self.password = v5, username, password
        self.prefs = {
            "proxy_type": "None" if v5 else -1, "proxy_ip": "0.0.0.0", "proxy_port": 8080,
            "proxy_auth_enabled": False, "proxy_peer_connections": False, "proxy_bittorrent": False,
            "proxy_misc": True, "proxy_rss": True, "proxy_hostname_lookup": False,
            "bittorrent_protocol": 0, "max_connec": 300, "max_connec_per_torrent": 100,
            "save_path": "C:/Downloads",
        }
        self.calls = []
        self.torrents = [{"hash": "a" * 40, "state": "downloading"}, {"hash": "b" * 40, "state": "downloading"}]
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok(self, body=b"Ok.", ctype="text/plain", cookie=None):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                return not outer.username or "SID=good" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.calls.append(("GET", path))
                if not self._authed():
                    self.send_response(403); self.end_headers(); return
                if path == "/api/v2/app/version":
                    self._ok(b"v5.2.4" if outer.v5 else b"v4.6.7")
                elif path == "/api/v2/app/preferences":
                    self._ok(json.dumps(outer.prefs).encode(), "application/json")
                elif path == "/api/v2/torrents/info":
                    self._ok(json.dumps(outer.torrents).encode(), "application/json")
                else:
                    self.send_response(404); self.end_headers()

            def do_POST(self):
                path = self.path.split("?")[0]
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
                outer.calls.append(("POST", path))
                if path == "/api/v2/auth/login":
                    if form.get("username") == outer.username and form.get("password") == outer.password:
                        return self._ok(cookie="SID=good; path=/")
                    return self._ok(b"Fails.")
                if not self._authed():
                    self.send_response(403); self.end_headers(); return
                if path == "/api/v2/app/setPreferences":
                    outer.prefs.update(json.loads(form["json"]))
                    self._ok()
                elif path in (("/api/v2/torrents/stop", "/api/v2/torrents/start") if outer.v5
                              else ("/api/v2/torrents/pause", "/api/v2/torrents/resume")):
                    self._ok()
                else:
                    self.send_response(404); self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
