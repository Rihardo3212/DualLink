"""The engine's local control API.

The background engine (proxy, balancing, tray icon) and the window are separate processes. The window - and
later a native window - asks the engine for everything over a small HTTP API on 127.0.0.1:

    POST http://127.0.0.1:<port>/api/<name>      X-DualLink-Token: <token>      {"args": [...]}
    -> {"ok": true, "result": ...}   or   {"ok": false, "error": "..."}

The port and token are in %APPDATA%\\DualLink\\engine.json, which only this Windows user can read. Requests
that come from a web page (they carry an Origin header) or name another host are refused, so websites open in
a browser can't reach it.
"""
import hmac
import http.client
import inspect
import json
import os
import queue
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .paths import config_dir

TOKEN_HEADER = "X-DualLink-Token"
MAX_BODY = 1 << 20


def engine_file():
    return config_dir() / "engine.json"


class Api:
    """What the interface can ask for (in the page: window.pywebview.api.<name>())."""

    PAGE = ("get_init", "status", "refresh_adapters", "save_settings", "power", "retry_qbt", "test_qbt",
            "start_speed_test", "check_updates", "start_update", "open_url", "traffic_check", "copy")

    def __init__(self, ctl):
        self._ctl = ctl

    def get_init(self):
        adapters = self._ctl.adapters()
        return {"settings": self._ctl.public_settings(), "adapters": adapters}

    def status(self):
        return self._ctl.status(for_ui=True)

    def refresh_adapters(self):
        adapters = self._ctl.adapters()
        return {"settings": self._ctl.public_settings(), "adapters": adapters}

    def save_settings(self, new):
        from .controller import SettingsError, set_autostart
        before = bool(self._ctl.cfg.get("autostart"))
        try:
            settings = self._ctl.update_settings(new)
        except SettingsError as e:
            return {"settings": self._ctl.public_settings(), "error": str(e)}
        except (TypeError, ValueError, AttributeError):
            return {"settings": self._ctl.public_settings(), "error": "Some settings were not valid numbers."}
        error = None
        if bool(settings.get("autostart")) != before:
            try:
                set_autostart(bool(settings["autostart"]))
            except Exception as e:  # noqa: BLE001
                error = f"Couldn't change 'Start with Windows': {e}"
        return {"settings": settings, "error": error}

    def power(self, on):
        return self._ctl.power_on() if on else self._ctl.power_off()

    def retry_qbt(self):
        return self._ctl.retry_qbt()

    def test_qbt(self):
        return self._ctl.test_qbt()

    def start_speed_test(self):
        return self._ctl.start_speed_test()

    def check_updates(self):
        return self._ctl.check_updates_now()

    def start_update(self):
        return self._ctl.start_update()

    def open_url(self, url):
        return open_github_url(url)

    def traffic_check(self):
        return self._ctl.traffic_check()

    def copy(self, text):
        return copy_text(text)


def open_github_url(url):
    if not str(url).startswith("https://github.com/"):
        return False                      # only ever open our own release pages
    import webbrowser
    webbrowser.open(url)
    return True


def copy_text(text):
    """Clipboard fallback for the setup steps (navigator.clipboard may be blocked for local pages)."""
    try:
        import subprocess
        subprocess.run(["clip"], input=str(text).encode("ascii", "ignore"), check=True, creationflags=0x08000000)
        return True
    except Exception:
        return False


# ---- server (runs in the engine) ------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False           # on Windows that would let another program share the port

    def __init__(self, *a, **kw):
        self.open_sockets = set()
        self.sock_lock = threading.Lock()
        super().__init__(*a, **kw)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        with self.sock_lock:
            self.open_sockets.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        with self.sock_lock:
            self.open_sockets.discard(request)
        super().shutdown_request(request)

    def close_all(self):
        """Also end kept-alive connections, so nothing is answered after stop()."""
        with self.sock_lock:
            socks = list(self.open_sockets)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ControlServer:
    def __init__(self, handlers, token=None, port=0):
        self.handlers = dict(handlers)
        self.token = token or secrets.token_urlsafe(32)
        self.info_path = None
        self.stopped = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "DualLink"
            sys_version = ""

            def log_message(self, *args):
                pass

            def do_GET(self):
                self._reply(405, {"ok": False, "error": "Use POST."})

            do_PUT = do_DELETE = do_PATCH = do_GET

            def do_POST(self):
                code, body = outer._dispatch(self)
                self._reply(code, body)

            def _reply(self, code, body):
                data = json.dumps(body, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                if code != 200:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

        self.httpd = _Server(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = None

    def _dispatch(self, req):
        try:
            length = int(req.headers.get("Content-Length") or 0)
        except ValueError:
            return 400, {"ok": False, "error": "Bad request."}
        if length > MAX_BODY:
            return 413, {"ok": False, "error": "Too large."}
        raw = req.rfile.read(length) if length > 0 else b""
        if self.stopped:
            return 503, {"ok": False, "error": "DualLink is closing."}
        if req.headers.get("Origin") is not None:            # sent by browsers; DualLink's own windows never do
            return 403, {"ok": False, "error": "Forbidden."}
        if req.headers.get("Host", "") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return 403, {"ok": False, "error": "Forbidden."}
        given = req.headers.get(TOKEN_HEADER, "").encode("utf-8", "replace")
        if not hmac.compare_digest(given, self.token.encode("utf-8")):
            return 403, {"ok": False, "error": "Forbidden."}
        name = req.path[len("/api/"):] if req.path.startswith("/api/") else ""
        fn = self.handlers.get(name)
        if fn is None:
            return 404, {"ok": False, "error": f"Unknown request '{name}'."}
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            args = body.get("args", []) if isinstance(body, dict) else None
            if not isinstance(args, list):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return 400, {"ok": False, "error": "Bad request."}
        try:
            return 200, {"ok": True, "result": fn(*args)}
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                        daemon=True, name="control-api")
        self._thread.start()
        return self

    def publish(self, path=None):
        """Write engine.json so a window can find this engine."""
        path = path or engine_file()
        info = {"port": self.port, "token": self.token, "pid": os.getpid(), "version": __version__}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, path)
        self.info_path = path
        return info

    def unpublish(self):
        path, self.info_path = self.info_path, None
        if not path:
            return
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass

    def stop(self):
        self.stopped = True
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd.close_all()
        except Exception:
            pass


# ---- client (runs in the window) ------------------------------------------------------------ #
class EngineError(Exception):
    pass


class EngineClient:
    TIMEOUTS = {"traffic_check": 90, "test_qbt": 40, "check_updates": 40, "refresh_adapters": 40,
                "get_init": 40, "power": 60, "retry_qbt": 60, "save_settings": 60}

    def __init__(self, port, token, pid=None, version=None, timeout=15.0):
        self.port, self.token, self.pid, self.version = int(port), str(token), pid, version
        self.timeout = timeout
        self._pool = queue.LifoQueue()

    @classmethod
    def from_file(cls, path=None):
        try:
            info = json.loads((path or engine_file()).read_text(encoding="utf-8"))
            return cls(info["port"], info["token"], info.get("pid"), info.get("version"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @classmethod
    def connect(cls, path=None, wait=0.0):
        """The running engine, or None. With wait, keep trying (the engine may still be starting)."""
        end = time.monotonic() + wait
        while True:
            client = cls.from_file(path)
            if client is not None:
                try:
                    client.call("ping", timeout=3)
                    return client
                except EngineError:
                    client.close()
            if time.monotonic() >= end:
                return None
            time.sleep(0.25)

    def _conn(self, timeout):
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        return conn

    def call(self, name, *args, timeout=None):
        timeout = timeout or self.TIMEOUTS.get(name, self.timeout)
        body = json.dumps({"args": list(args)}).encode("utf-8")
        headers = {"Content-Type": "application/json", TOKEN_HEADER: self.token}
        for attempt in (1, 2):
            conn = self._conn(timeout)
            reused = conn.sock is not None
            try:
                conn.request("POST", f"/api/{name}", body, headers)
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                if attempt == 1 and reused and not isinstance(e, TimeoutError):
                    continue              # a kept-alive connection had gone stale: try once on a fresh one
                raise EngineError("DualLink's background engine isn't responding.") from e
            if resp.will_close:
                conn.close()
            else:
                self._pool.put(conn)
            try:
                reply = json.loads(data.decode("utf-8"))
            except ValueError as e:
                raise EngineError("Unexpected reply from DualLink's engine.") from e
            if not reply.get("ok"):
                raise EngineError(reply.get("error") or "DualLink's engine refused the request.")
            return reply.get("result")

    def close(self):
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                return


def _remote(name, signature):
    def method(self, *args):
        return self._client.call(name, *args)
    method.__name__ = method.__qualname__ = name
    method.__signature__ = signature              # same parameters as Api.<name>, for pywebview
    return method


class RemoteApi:
    """The window's js_api: the same methods as Api, each one answered by the engine."""

    def __init__(self, client, visible=lambda: True):
        self._client = client
        self._visible = visible

    def status(self):
        if not self._visible():
            return {"hidden": True}               # minimised: nothing to draw, don't ask the engine
        return self._client.call("status")

    # done by the window itself: it's in front, so the browser it opens comes to the front too
    def open_url(self, url):
        return open_github_url(url)

    def copy(self, text):
        return copy_text(text)


for _name in Api.PAGE:
    if _name not in RemoteApi.__dict__:
        setattr(RemoteApi, _name, _remote(_name, inspect.signature(getattr(Api, _name))))
del _name
