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
    api_ok = False
    if os.name == "nt":
        try:                                    # ask Windows directly (no ipconfig process)
            from . import winapi
            found = [(a["name"], ip) for a in winapi.adapters() for ip in a["v4"]]
            api_ok = True
        except Exception:
            found = []
    if os.name == "nt" and not api_ok:
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
        self.ip6 = ""             # the adapter's global IPv6 address, if it has one
        self.weight = 1.0
        self.quota_bytes = 0          # 0 = unlimited (applies to down+up)
        self.active = 0
        self.pending = 0          # connections being opened right now
        self.pending_at = {}      # attempt -> when it started (only fresh ones count as load, see order())
        self.udp_bytes = 0        # DHT / UDP-tracker traffic (kept out of the speed measurements)
        self.down = 0
        self.up = 0
        self.failures = 0
        self.enabled = True
        self.rate = 0.0          # smoothed download speed of this link, bytes/s
        self.per_conn = 0.0      # smoothed download speed per connection, bytes/s
        self._last_down = 0
        self.measured_at = -1e9  # last time this link had a connection actually carrying data

    def over_quota(self):
        return self.quota_bytes > 0 and (self.down + self.up + self.udp_bytes) >= self.quota_bytes

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


class _UdpAssociation:
    """One SOCKS5 UDP association: client <-> relay socket <-> per-link outbound sockets."""

    def __init__(self, proxy, loop):
        self.proxy, self.loop = proxy, loop
        self.relay = None
        self.client = None                      # (ip, port) qBittorrent sends from
        self.out = {}                           # id(link) -> transport bound to that link's IP
        self.dns = {}                           # hostname -> ip
        self.closed = False

    async def _outbound(self, link):
        key = (id(link), link.ip)           # a new IP (reconnected phone) needs a new socket
        t = self.out.get(key)
        if t is None:
            t, _ = await self.loop.create_datagram_endpoint(
                lambda: _RemoteSide(self, link), local_addr=(link.ip or "0.0.0.0", 0))
            self.out[key] = t
        return t

    def from_client(self, data, addr):
        if self.client is None:
            self.client = addr
        if addr != self.client or len(data) < 10 or data[2] != 0:   # wrong sender, or fragmented
            return
        atyp = data[3]
        try:
            if atyp == 1:
                host, i = socket.inet_ntoa(data[4:8]), 8
            elif atyp == 3:
                n = data[4]
                host, i = data[5:5 + n].decode("ascii", "ignore"), 5 + n
            else:
                return                                   # IPv6 destinations: not supported
            port = struct.unpack(">H", data[i:i + 2])[0]
        except Exception:
            return
        payload = data[i + 2:]
        self.loop.create_task(self._send(host, port, payload, atyp == 3))

    async def _send(self, host, port, payload, is_name):
        if self.closed:
            return
        try:
            if is_name:
                ip = self.dns.get(host)
                if ip is None:
                    infos = await self.loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
                    ip = self.dns[host] = infos[0][4][0]
                host = ip
            link = self.proxy.udp_link()
            if link is None or self.closed:
                return
            t = await self._outbound(link)
            t.sendto(payload, (host, port))
            link.udp_bytes += len(payload)
            self.proxy.udp_packets += 1
        except Exception:
            pass

    def from_remote(self, link, data, addr):
        if self.closed or self.client is None or self.relay is None:
            return
        link.udp_bytes += len(data)
        header = b"\x00\x00\x00\x01" + socket.inet_aton(addr[0]) + struct.pack(">H", addr[1])
        try:
            self.relay.sendto(header + data, self.client)
        except Exception:
            pass

    def close(self):
        self.closed = True
        for t in list(self.out.values()) + ([self.relay] if self.relay else []):
            try:
                t.close()
            except Exception:
                pass


class _RelaySide(asyncio.DatagramProtocol):
    def __init__(self, assoc):
        self.assoc = assoc

    def datagram_received(self, data, addr):
        self.assoc.from_client(data, addr)


class _RemoteSide(asyncio.DatagramProtocol):
    def __init__(self, assoc, link):
        self.assoc, self.link = assoc, link

    def datagram_received(self, data, addr):
        self.assoc.from_remote(self.link, data, addr)

    def error_received(self, exc):
        pass   # ICMP "port unreachable" from a dead DHT node: normal, ignore


class _PendingConn:
    """Stands in for Conn during the few microseconds before the connection is registered."""
    __slots__ = ("link",)

    def __init__(self, link):
        self.link = link

    def add(self, n):
        pass


class _Pipe(asyncio.Protocol):
    """One direction of a relayed connection: whatever arrives is written straight to the other side."""
    __slots__ = ("link", "conn", "down", "done", "peer", "transport")

    def __init__(self, link, conn, down, done):
        self.link, self.conn, self.down, self.done = link, conn, down, done
        self.peer = self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        n = len(data)
        if self.down:
            self.link.down += n
        else:
            self.link.up += n
        self.conn.add(n)
        self.peer.transport.write(data)
        if self.link.quota_bytes and self.link.over_quota():
            self.transport.close()
            self.peer.transport.close()

    def eof_received(self):
        return False                      # close this side; connection_lost then closes the other

    def pause_writing(self):              # our outgoing buffer is full: stop reading from the other side
        if self.peer.transport is not None:
            self.peer.transport.pause_reading()

    def resume_writing(self):
        if self.peer.transport is not None:
            self.peer.transport.resume_reading()

    def connection_lost(self, exc):
        if self.peer is not None and self.peer.transport is not None:
            self.peer.transport.close()
        if not self.done.done():
            self.done.set_result(None)


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
        self.udp_assocs = 0       # open UDP associations (qBittorrent's DHT / UDP trackers)
        # Pass-through: DualLink is "off" but apps like Free Download Manager stay pointed at the proxy,
        # so connections go out normally (Windows picks the route) instead of failing.
        self.passthrough = False
        self.direct = Link("Direct")
        self.refused_ipv6 = 0     # CONNECT requests to IPv6 addresses (not supported yet)
        self.failed = 0           # connections that couldn't be made on any link
        self.udp_packets = 0

    # -- link choice -------------------------------------------------------- #
    # Attempts being opened count as load, so a burst of new connections spreads over the links. But only
    # fresh ones: an attempt still unanswered after this long is almost always to a peer that isn't there
    # (torrents try many), and if those piled up on a link with slower failures (a phone's mobile network)
    # they would push the working connections onto the other link.
    FRESH_PENDING = 1.5

    def order(self):
        if self.passthrough:
            return [self.direct]
        usable = [l for l in self.links if l.usable()]
        now = time.monotonic()

        def load(l):
            fresh = sum(1 for t in list(l.pending_at.values()) if now - t < self.FRESH_PENDING)
            return (l.active + fresh + 1) / max(l.weight, 0.01)
        return sorted(usable, key=load)

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
                if self.passthrough or not (self.auto and self.recycle and best) or now - last_move < self.MOVE_EVERY:
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

    def close_link(self, link):
        """Close every connection on a link (adapter gone or new IP); qBittorrent reconnects them."""
        loop = self.loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: [c.task.cancel() for c in list(self.conns) if c.link is link])

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
        remote_t = None
        link = None
        conn = None
        try:
            ver, n = await reader.readexactly(2)
            if ver != 5:
                return
            await reader.readexactly(n)
            writer.write(b"\x05\x00")
            ver, cmd, _, atyp = await reader.readexactly(4)
            if cmd not in (1, 3):
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            v6 = False
            if atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 3:
                ln = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(ln)).decode("ascii", "ignore")
            elif atyp == 4:
                raw = await reader.readexactly(16)
                host = "" if cmd == 3 else socket.inet_ntop(socket.AF_INET6, raw)   # UDP: just a hint
                v6 = cmd == 1
            else:
                writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            port = struct.unpack(">H", await reader.readexactly(2))[0]
            if cmd == 3:
                await self._udp_associate(reader, writer)
                return

            can_v6 = self.passthrough or any(l.usable() and l.ip6 for l in self.links)
            if v6 and not can_v6:
                self.refused_ipv6 += 1
                writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")  # no link has IPv6 right now
                return
            loop = asyncio.get_running_loop()
            try:
                if v6:
                    dest = (host, port, 0, 0)
                else:
                    try:     # IPv4 first; fall back to IPv6 for names that only have an IPv6 address
                        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
                    except Exception:
                        if atyp != 3 or not can_v6:
                            raise
                        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET6, type=socket.SOCK_STREAM)
                        v6 = True
                    dest = infos[0][4]
            except Exception:
                writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            sock = None
            tried = set()
            while sock is None:
                # pick again after every failure: the best link may have changed meanwhile
                choices = [l for l in self.order() if id(l) not in tried and (not v6 or l.ip6 or not l.ip)]
                if not choices:
                    break
                cand = choices[0]
                tried.add(id(cand))
                cand.pending += 1
                attempt = object()
                cand.pending_at[attempt] = time.monotonic()
                s = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_STREAM)
                s.setblocking(False)
                try:
                    if v6 and cand.ip6:      # the "Direct" pass-through link isn't bound
                        s.bind((cand.ip6, 0, 0, 0))
                    elif not v6 and cand.ip:
                        s.bind((cand.ip, 0))
                    await asyncio.wait_for(loop.sock_connect(s, dest), 10)
                    sock, link = s, cand
                except Exception:
                    cand.failures += 1
                    s.close()
                finally:
                    cand.pending -= 1
                    cand.pending_at.pop(attempt, None)
            if sock is None:
                self.failed += 1
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            # Data phase: hand both sockets to _Pipe callbacks, which pass data straight through
            # (much less CPU than awaiting every chunk; flow control via pause/resume_reading).
            done = loop.create_future()
            pending = _PendingConn(link)
            up = _Pipe(link, pending, False, done)          # app -> internet
            down = _Pipe(link, pending, True, done)         # internet -> app
            up.peer, down.peer = down, up
            remote_t, _ = await loop.create_connection(lambda: down, sock=sock)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            client_t = writer.transport
            leftover = bytes(reader._buffer)                # anything the app sent right after its request
            reader._buffer.clear()
            client_t.set_protocol(up)
            up.connection_made(client_t)
            if not client_t.is_reading():                   # the stream reader may have paused it
                client_t.resume_reading()
            for t in (client_t, remote_t):
                t.set_write_buffer_limits(high=256 * 1024)
            conn = Conn(link, asyncio.current_task())
            up.conn = down.conn = conn
            self.conns.add(conn)
            link.active += 1
            if leftover:
                up.data_received(leftover)
            await done
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if conn is not None:
                link.active -= 1
                self.conns.discard(conn)
            for t in (writer, remote_t):
                if t is not None:
                    try:
                        t.close()
                    except Exception:
                        pass

    # -- SOCKS5 UDP ASSOCIATE (DHT, UDP trackers) ------------------------------ #
    def udp_link(self):
        """UDP is many tiny packets: keep it on the first link (Ethernet) to save mobile data."""
        if self.passthrough:
            return self.direct
        return next((l for l in self.links if l.usable()), None)

    async def _udp_associate(self, reader, writer):
        loop = asyncio.get_running_loop()
        assoc = _UdpAssociation(self, loop)
        relay_t, _ = await loop.create_datagram_endpoint(
            lambda: _RelaySide(assoc), local_addr=("127.0.0.1", 0))
        assoc.relay = relay_t
        port = relay_t.get_extra_info("sockname")[1]
        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", port))
        self.udp_assocs += 1
        try:
            while await reader.read(4096):   # the association lives as long as this TCP connection
                pass
        finally:
            self.udp_assocs -= 1
            assoc.close()



