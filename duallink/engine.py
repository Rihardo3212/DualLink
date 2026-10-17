"""
DualLink engine: SOCKS5 proxy that spreads outgoing connections over two network links,
automatically favouring the faster one. No third-party dependencies.
"""
import asyncio
import os
import socket
import struct
import subprocess
import threading
import time



# --------------------------------------------------------------------------- #
# Network adapter discovery
# --------------------------------------------------------------------------- #
def list_adapters():
    """Return [(adapter_name, ipv4)] for usable IPv4 addresses."""
    found = []
    if os.name == "nt":
        try:
            txt = subprocess.run(["ipconfig"], capture_output=True, errors="ignore", text=True,
                                 timeout=10, creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
        except Exception:
            txt = ""
        name = "adapter"
        for line in txt.splitlines():
            if line.strip() and not line[0].isspace() and line.rstrip().endswith(":"):
                name = line.rstrip()[:-1].strip()
            elif "IPv4" in line and ":" in line:
                ip = line.split(":")[-1].split("(")[0].strip()
                found.append((name, ip))
    if not found:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.append(("adapter", info[4][0]))
        except Exception:
            pass
    seen, out = set(), []
    for name, ip in found:
        if ip in seen or ip.startswith(("127.", "169.254.")) or ip.count(".") != 3:
            continue
        seen.add(ip)
        out.append((name, ip))
    return out


def looks_like_hotspot(name: str, ip: str) -> bool:
    low = name.lower()
    return (ip.startswith(("192.168.43.", "172.20.10.", "192.168.42.", "192.168.137."))
            or any(k in low for k in ("rndis", "wi-fi", "wifi", "wireless", "wlan", "mobile", "tether", "usb")))


# --------------------------------------------------------------------------- #
# Proxy core
# --------------------------------------------------------------------------- #
class Link:
    def __init__(self, name):
        self.name = name
        self.ip = ""
        self.weight = 1.0
        self.quota_bytes = 0          # 0 = unlimited (applies to down+up)
        self.active = 0
        self.pending = 0          # connections being opened right now (counted so bursts spread out)
        self.down = 0
        self.up = 0
        self.failures = 0
        self.enabled = True
        self.rate = 0.0          # smoothed download speed of this link, bytes/s
        self.per_conn = 0.0      # smoothed download speed per connection, bytes/s
        self._last_down = 0
        self.measured_at = -1e9  # last time this link had a connection actually carrying data

    def over_quota(self):
        return self.quota_bytes > 0 and (self.down + self.up) >= self.quota_bytes

    def usable(self):
        return bool(self.ip) and self.enabled and not self.over_quota()


class Conn:
    __slots__ = ("link", "task", "started", "last_busy", "bytes", "mark")

    def __init__(self, link, task):
        self.link = link
        self.task = task
        self.started = self.last_busy = time.monotonic()
        self.bytes = self.mark = 0

    def add(self, n):
        self.bytes += n
        if self.bytes - self.mark >= 16384:
            self.mark = self.bytes
            self.last_busy = time.monotonic()


class BalancingProxy:
    """SOCKS5 (CONNECT only) proxy; each outgoing connection is bound to one link's local IP."""

    def __init__(self, links):
        self.links = links
        self.loop = None
        self.server = None
        self.thread = None
        self.port = 0
        self.error = None
        self._ready = threading.Event()
        self.auto = True          # adapt weights to measured speed
        self.recycle = True       # move idle connections to the faster link
        self.min_share = 0.15     # the slower link always keeps at least this share (keeps measuring it)
        self.conns = set()
        self.moved = 0

    # -- link choice -------------------------------------------------------- #
    def order(self):
        usable = [l for l in self.links if l.usable()]
        return sorted(usable, key=lambda l: (l.active + l.pending + 1) / max(l.weight, 0.01))

    # -- lifecycle ---------------------------------------------------------- #
    def start(self, port: int):
        self.error = None
        self._ready.clear()
        self.thread = threading.Thread(target=self._run, args=(port,), daemon=True)
        self.thread.start()
        self._ready.wait(15)
        if self.error:
            raise RuntimeError(self.error)

    def _run(self, port):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        last = None
        for _ in range(12):  # old listener may still be closing: retry for ~6 s
            try:
                self.server = self.loop.run_until_complete(
                    asyncio.start_server(self._handle, "127.0.0.1", port))
                self.port = self.server.sockets[0].getsockname()[1]
                last = None
                break
            except OSError as e:
                last = e
                time.sleep(0.5)
        if last is not None:
            self.error = (f"Could not listen on port {port}: {last}. "
                          f"Another program (or a copy of DualLink) is using it - try a different port.")
            self.loop.close()
            self._ready.set()
            return
        self._ready.set()
        self.loop.create_task(self._balance_loop())
        self.loop.run_forever()
        self.loop.close()

    # -- automatic balancing ------------------------------------------------ #
    BAL_PERIOD = 3.0       # seconds between measurements
    IDLE_AFTER = 90.0      # a connection moving <16 KiB for this long counts as idle
    MIN_AGE = 120.0        # never touch connections younger than this
    MOVE_EVERY = 15.0      # move at most one idle connection per this many seconds

    def _update_weights(self):
        a = 0.35
        now = time.monotonic()
        busy = {id(l): 0 for l in self.links}
        for c in self.conns:   # only count connections that actually moved data in the last 10 s
            if c.mark > 0 and now - c.last_busy < 10:
                busy[id(c.link)] += 1
        for l in self.links:
            delta = l.down - l._last_down
            l._last_down = l.down
            inst = delta / self.BAL_PERIOD
            l.rate = (1 - a) * l.rate + a * inst
            if busy[id(l)] > 0:
                l.per_conn = (1 - a) * l.per_conn + a * (inst / busy[id(l)])
                l.measured_at = now
        if not self.auto:
            return
        usable = [l for l in self.links if l.usable()]
        total = sum(l.per_conn for l in usable)
        # A link with no data-carrying connection in the last 60 s is "unknown", not "slow":
        # keep an even split until both links have been measured.
        unknown = any(now - l.measured_at > 60 for l in usable)
        if len(usable) < 2 or unknown or total < 32 * 1024:   # not enough data yet: split evenly
            for l in self.links:
                l.weight = 1.0
            return
        for l in usable:
            share = l.per_conn / total
            l.weight = min(max(share, self.min_share), 1 - self.min_share)

    def preferred(self):
        usable = [l for l in self.links if l.usable()]
        if len(usable) < 2:
            return None
        total = sum(l.weight for l in usable)
        best = max(usable, key=lambda l: l.weight)
        # only act when one link is clearly better (>= 60% share)
        return best if best.weight / total >= 0.6 else None

    async def _balance_loop(self):
        last_move = 0.0
        while True:
            await asyncio.sleep(self.BAL_PERIOD)
            try:
                self._update_weights()
                now = time.monotonic()
                best = self.preferred()
                if not (self.auto and self.recycle and best) or now - last_move < self.MOVE_EVERY:
                    continue
                idle = [c for c in self.conns
                        if c.link is not best and now - c.started > self.MIN_AGE
                        and now - c.last_busy > self.IDLE_AFTER]
                if idle:
                    victim = min(idle, key=lambda c: c.last_busy)
                    victim.task.cancel()   # qBittorrent reconnects; the new connection goes to the faster link
                    self.moved += 1
                    last_move = now
            except Exception:
                pass

    async def _shutdown(self):
        self.server.close()
        me = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not me]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.get_running_loop().stop()

    def stop(self):
        loop, thread = self.loop, self.thread
        if loop and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            except Exception:
                pass
        if thread:
            thread.join(6)
        self.server = None

    # -- SOCKS5 -------------------------------------------------------------- #
    async def _handle(self, reader, writer):
        remote_w = None
        link = None
        conn = None
        try:
            ver, n = await reader.readexactly(2)
            if ver != 5:
                return
            await reader.readexactly(n)
            writer.write(b"\x05\x00")
            ver, cmd, _, atyp = await reader.readexactly(4)
            if cmd != 1:
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            if atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 3:
                ln = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(ln)).decode("idna", "ignore")
            else:
                writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")  # IPv6 not supported
                return
            port = struct.unpack(">H", await reader.readexactly(2))[0]

            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
                dest = infos[0][4]
            except Exception:
                writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            sock = None
            tried = set()
            while sock is None:
                # pick again after every failure: the best link may have changed meanwhile
                choices = [l for l in self.order() if id(l) not in tried]
                if not choices:
                    break
                cand = choices[0]
                tried.add(id(cand))
                cand.pending += 1
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setblocking(False)
                try:
                    s.bind((cand.ip, 0))
                    await asyncio.wait_for(loop.sock_connect(s, dest), 10)
                    sock, link = s, cand
                except Exception:
                    cand.failures += 1
                    s.close()
                finally:
                    cand.pending -= 1
            if sock is None:
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            remote_r, remote_w = await asyncio.open_connection(sock=sock)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            conn = Conn(link, asyncio.current_task())
            self.conns.add(conn)
            link.active += 1
            await asyncio.gather(
                self._pump(reader, remote_w, link, "up", conn),
                self._pump(remote_r, writer, link, "down", conn),
            )
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if conn is not None:
                link.active -= 1
                self.conns.discard(conn)
            for w in (writer, remote_w):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    async def _pump(self, r, w, link, direction, conn):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                conn.add(len(data))
                if direction == "down":
                    link.down += len(data)
                else:
                    link.up += len(data)
                w.write(data)
                await w.drain()
                if link.over_quota():
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass


