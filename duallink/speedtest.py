"""
Speed test: how fast can each connection download on its own?

Downloads from a test server over one link (several connections bound to that link's address) for a
few seconds and measures the throughput after a short warm-up.
"""
import http.client
import threading
import time
import urllib.parse

# Cloudflare's public speed-test endpoint (the one speed.cloudflare.com uses).
TEST_URL = "https://speed.cloudflare.com/__down?bytes=50000000"


def measure(source_ip, seconds=5.0, streams=4, url=TEST_URL, warmup=1.0, timeout=8):
    """Download over the link with this local IP. Returns {"bps": bytes_per_second, "error": str|None}."""
    u = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    path = u.path + ("?" + u.query if u.query else "")
    lock = threading.Lock()
    total = [0]
    errors = []
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            conn = None
            try:
                conn = conn_cls(u.hostname, u.port, timeout=timeout, source_address=(source_ip, 0))
                conn.request("GET", path, headers={"User-Agent": "DualLink speed test", "Cache-Control": "no-cache"})
                r = conn.getresponse()
                if r.status != 200:
                    errors.append(f"The test server answered {r.status}.")
                    return
                while not stop.is_set():
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    with lock:
                        total[0] += len(chunk)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e) or e.__class__.__name__)
                return
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(streams)]
    for t in threads:
        t.start()
    time.sleep(warmup)                       # skip the slow start of each connection
    with lock:
        b0, t0 = total[0], time.monotonic()
    time.sleep(max(seconds - warmup, 0.5))
    with lock:
        b1, t1 = total[0], time.monotonic()
    stop.set()
    for t in threads:
        t.join(timeout=2)
    bps = (b1 - b0) / max(t1 - t0, 0.001)
    error = None
    if b1 == 0:
        error = errors[0] if errors else "No data arrived."
    return {"bps": bps, "error": error}
