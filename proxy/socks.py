#!/usr/bin/env python3

import os
import sys
import signal
import asyncio
import logging
import socket as _socket
import random
import time
from typing import Optional, Set, List, Dict, Tuple, Deque
from urllib.parse import urlencode
from collections import deque

if __name__ == '__main__' and (__package__ is None or __package__ == ''):
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    __package__ = 'proxy'

from .raw_websocket import RawWebSocket, WsHandshakeError
from .config import proxy_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-5s [SOCKS5] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('socks5')


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '').strip() or default)
    except (ValueError, AttributeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, '').strip() or default)
    except (ValueError, AttributeError):
        return default


SOCKS_HOST = os.environ.get('SOCKS_HOST', '0.0.0.0')
SOCKS_PORT = _env_int('SOCKS_PORT', 1080)

# --- Tunables (all optional; defaults preserve prior behaviour where it matters) ---
# Liveness is measured across BOTH directions, so a pure download (client silent)
# is not killed. This fixes the old per-direction 90s read timeout.
WS_IDLE_TIMEOUT = _env_float('SOCKS_IDLE_TIMEOUT', 180.0)
IDLE_POLL = 1.0
DRAIN_TIMEOUT = 30.0
DNS_TIMEOUT = _env_float('SOCKS_DNS_TIMEOUT', 3.0)
# Whole WS-handshake budget. Must stay below the client's SOCKS connect timeout
# (rss-bridge TgWSProxy uses CONNECTTIMEOUT=5s).
CONNECT_BUDGET = _env_float('SOCKS_CONNECT_BUDGET', 3.5)
PREWARM_CONNECT_BUDGET = _env_float('SOCKS_PREWARM_CONNECT_BUDGET', 8.0)
# Concurrency gate: max simultaneous client tunnels; short queue then fast-fail.
MAX_CONNS = max(1, _env_int('SOCKS_MAX_CONNS', 6))
QUEUE_TIMEOUT = _env_float('SOCKS_QUEUE_TIMEOUT', 1.5)
# Pre-connect pool (safe: connections never carry client data before handoff,
# exactly like proxy/pool.py on the MTProto side). 0 disables prewarm.
PREWARM_PER_TARGET = max(0, _env_int('SOCKS_PREWARM_PER_TARGET', 1))
PREWARM_INTERVAL = _env_float('SOCKS_PREWARM_INTERVAL', 20.0)
TARGET_TTL = _env_float('SOCKS_TARGET_TTL', 300.0)
POOL_MAX_AGE = _env_float('SOCKS_POOL_MAX_AGE', 90.0)
POOL_MAX_TOTAL = max(0, _env_int('SOCKS_POOL_MAX_TOTAL', 8))
# WS-level ping to keep idle keep-alive tunnels from being closed by CF (~100s).
PING_INTERVAL = _env_float('SOCKS_PING_INTERVAL', 30.0)
# Optional socket buffer override for THIS process (proxy_config here is the
# dataclass default, not the --buf-kb passed to tg_ws_proxy.py).
BUF_KB = _env_int('SOCKS_BUF_KB', 0)

WS_MAX_FRAME_SIZE = 64 * 1024
HANDSHAKE_READ_TIMEOUT = 10.0

# Per-domain circuit breaker (mirrors keepalive.py's approach).
BREAKER_MAX_FAILURES = 3
BREAKER_COOLDOWN_BASE = 30.0
BREAKER_COOLDOWN_MAX = 600.0


def _parse_domains(value: str) -> List[str]:
    items = value.replace(',', ' ').replace(';', ' ').split()
    seen = set()
    result = []
    for item in items:
        item = item.strip().lower()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


WORKER_DOMAINS = _parse_domains(os.environ.get('CFPROXY_WORKER_DOMAIN', ''))

SOCKS_USER = os.environ.get('SOCKS_USER', '')
SOCKS_PASS = os.environ.get('SOCKS_PASS', '')
REQUIRE_AUTH = bool(SOCKS_USER and SOCKS_PASS)

_VER, _AUTH_NONE, _AUTH_PASSWORD, _AUTH_REJECT = 0x05, 0x00, 0x02, 0xFF
_CMD_CONNECT = 0x01
_ATYP_IPV4, _ATYP_DOMAIN, _ATYP_IPV6 = 0x01, 0x03, 0x04
_REP_OK, _REP_GENERAL_FAILURE, _REP_HOST_UNREACH = 0x00, 0x01, 0x04
_REP_CMD_UNSUPPORTED, _REP_ATYP_UNSUPPORTED = 0x07, 0x08
_REP_PORT_RESTRICTED = 0x02
_AUTH_VER, _AUTH_SUCCESS, _AUTH_FAILURE = 0x01, 0x00, 0xFF

_shutdown_event: Optional[asyncio.Event] = None
_conn_sem: Optional[asyncio.Semaphore] = None
_handler_tasks: Set[asyncio.Task] = set()


class _SocksStats:
    def __init__(self) -> None:
        self.total = 0
        self.active = 0
        self.rejected_busy = 0
        self.pool_hits = 0
        self.pool_misses = 0
        self.handshake_fail = 0
        self.bytes_up = 0
        self.bytes_down = 0

    def summary(self) -> str:
        pool_total = self.pool_hits + self.pool_misses
        pool_s = f"{self.pool_hits}/{pool_total}" if pool_total else "n/a"
        return (f"total={self.total} active={self.active}/{MAX_CONNS} "
                f"busy_reject={self.rejected_busy} pool={pool_s} "
                f"hs_fail={self.handshake_fail} "
                f"up={self.bytes_up} down={self.bytes_down}")


_stats = _SocksStats()


class WsConnectionPool:
    def __init__(self) -> None:
        self._idle: Dict[Tuple[str, int], Deque[Tuple[RawWebSocket, float]]] = {}
        self._lock = asyncio.Lock()
        self._recent: Dict[Tuple[str, int], float] = {}
        self._failures: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}

    # ---- circuit breaker ----
    def _ordered_domains(self, domains: List[str]) -> List[str]:
        now = time.monotonic()
        healthy = [d for d in domains if self._cooldown_until.get(d, 0.0) <= now]
        if healthy:
            random.shuffle(healthy)
            return healthy
        if not domains:
            return []
        # All cooling down: probe the one whose cooldown expires soonest.
        probe = min(domains, key=lambda d: self._cooldown_until.get(d, 0.0))
        return [probe]

    def _record_failure(self, domain: str, exc: BaseException) -> None:
        failures = self._failures.get(domain, 0) + 1
        self._failures[domain] = failures
        if failures >= BREAKER_MAX_FAILURES:
            cooldown = min(
                BREAKER_COOLDOWN_BASE * (2 ** (failures - BREAKER_MAX_FAILURES)),
                BREAKER_COOLDOWN_MAX,
            )
            if isinstance(exc, WsHandshakeError) and exc.status_code == 429:
                cooldown = max(cooldown, 120.0)
            cooldown *= random.uniform(0.8, 1.2)
            self._cooldown_until[domain] = time.monotonic() + cooldown
            log.warning("Worker %s failed %d times, cooldown %.0fs",
                        domain, failures, cooldown)

    def _record_success(self, domain: str) -> None:
        self._failures.pop(domain, None)
        self._cooldown_until.pop(domain, None)

    def cooldown_summary(self) -> str:
        now = time.monotonic()
        parts = []
        for d in WORKER_DOMAINS:
            until = self._cooldown_until.get(d, 0.0)
            if until > now:
                parts.append(f"{d}:{until - now:.0f}s")
        return ", ".join(parts) if parts else "none"

    # ---- staleness (same checks as proxy/pool.py) ----
    @staticmethod
    def _is_stale(ws: RawWebSocket, created: float, now: float) -> bool:
        return (now - created >= POOL_MAX_AGE
                or ws._closed
                or ws.writer.transport.is_closing()
                or ws.reader.at_eof()
                or ws.reader.exception() is not None)

    async def acquire(self, target_ip: str, target_port: int,
                      domains: List[str], budget: float) -> Optional[RawWebSocket]:
        key = (target_ip, target_port)
        self._recent[key] = time.monotonic()
        now = time.monotonic()
        async with self._lock:
            bucket = self._idle.get(key)
            if bucket:
                while bucket:
                    ws, created = bucket.popleft()
                    if self._is_stale(ws, created, now):
                        asyncio.create_task(self._quiet_close(ws))
                        continue
                    # Probe: a pre-connected tunnel must have received no data.
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        if data is None:
                            continue
                        asyncio.create_task(self._quiet_close(ws))
                        continue
                    except asyncio.TimeoutError:
                        _stats.pool_hits += 1
                        log.debug("Pool hit for %s:%d", target_ip, target_port)
                        return ws
                    except Exception:
                        continue

        _stats.pool_misses += 1
        return await self._create_new(target_ip, target_port, domains, budget)

    async def _connect_one(self, domain: str, target_ip: str,
                           path: str, timeout: float) -> RawWebSocket:
        return await asyncio.wait_for(
            RawWebSocket.connect(domain, domain, timeout=timeout, path=path),
            timeout=timeout + 0.5,
        )

    async def _create_new(self, target_ip: str, target_port: int,
                          domains: List[str], budget: float) -> Optional[RawWebSocket]:
        """Fast handshake: up to 2 domains in flight, first success wins,
        hard overall budget. Replaces the old 5 x N x 17s retry storm."""
        deadline = time.monotonic() + budget
        ordered = self._ordered_domains(domains)
        if not ordered:
            return None
        path = f"/apiws?{urlencode({'dst': target_ip})}"

        pending: Dict[asyncio.Task, str] = {}
        idx = 0

        def _seed() -> None:
            nonlocal idx
            while idx < len(ordered) and len(pending) < 2:
                remaining = deadline - time.monotonic()
                if remaining <= 0.2:
                    break
                d = ordered[idx]
                idx += 1
                pending[asyncio.create_task(
                    self._connect_one(d, target_ip, path, remaining))] = d

        _seed()
        result: Optional[RawWebSocket] = None
        try:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait(
                    pending.keys(), timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for t in done:
                    d = pending.pop(t)
                    try:
                        ws = t.result()
                    except Exception as exc:
                        _stats.handshake_fail += 1
                        self._record_failure(d, exc)
                        log.debug("WS fail via %s: %r", d, exc)
                        continue
                    if result is None:
                        result = ws
                        self._record_success(d)
                    else:
                        asyncio.create_task(self._quiet_close(ws))
                if result is not None:
                    break
                _seed()
            return result
        finally:
            for t in pending:
                t.cancel()
            if pending:
                gathered = await asyncio.gather(*pending, return_exceptions=True)
                for ws in gathered:
                    if isinstance(ws, RawWebSocket):
                        await self._quiet_close(ws)

    def release(self, ws: Optional[RawWebSocket], target_ip: str, target_port: int) -> None:
        # WS tunnels for raw TCP proxying cannot be safely pooled AFTER use:
        # the target TCP session may still be alive, and handing it to a new
        # client would send a fresh TLS ClientHello into an established session.
        # Pre-connect (before any client data) is safe and handled by prewarm.
        if ws is None or ws._closed:
            return
        asyncio.create_task(self._quiet_close(ws))
        log.debug("Closed WS for %s:%d after use (no reuse)", target_ip, target_port)

    async def prewarm(self) -> None:
        if PREWARM_PER_TARGET <= 0 or not WORKER_DOMAINS:
            return
        if _stats.active >= MAX_CONNS:
            return
        now = time.monotonic()
        keys = [k for k, ts in list(self._recent.items()) if now - ts <= TARGET_TTL]
        for key in keys:
            if _stats.active >= MAX_CONNS:
                break
            target_ip, target_port = key
            async with self._lock:
                bucket = self._idle.setdefault(key, deque())
                # drop stale
                fresh: Deque[Tuple[RawWebSocket, float]] = deque()
                while bucket:
                    ws, created = bucket.popleft()
                    if self._is_stale(ws, created, now):
                        asyncio.create_task(self._quiet_close(ws))
                    else:
                        fresh.append((ws, created))
                bucket.extend(fresh)
                needed = PREWARM_PER_TARGET - len(bucket)
                total_idle = sum(len(b) for b in self._idle.values())
                if total_idle >= POOL_MAX_TOTAL:
                    needed = 0
            for _ in range(max(0, needed)):
                ws = await self._create_new(target_ip, target_port,
                                            WORKER_DOMAINS, PREWARM_CONNECT_BUDGET)
                if ws is None:
                    break
                async with self._lock:
                    total_idle = sum(len(b) for b in self._idle.values())
                    if total_idle >= POOL_MAX_TOTAL:
                        asyncio.create_task(self._quiet_close(ws))
                        break
                    self._idle.setdefault(key, deque()).append((ws, time.monotonic()))
                log.debug("Prewarmed WS for %s:%d", target_ip, target_port)

    async def cleanup(self) -> None:
        async with self._lock:
            now = time.monotonic()
            to_close = []
            for key in list(self._idle.keys()):
                bucket = self._idle[key]
                new_bucket: Deque[Tuple[RawWebSocket, float]] = deque()
                while bucket:
                    ws, created = bucket.popleft()
                    if self._is_stale(ws, created, now):
                        to_close.append(ws)
                    else:
                        new_bucket.append((ws, created))
                if new_bucket:
                    self._idle[key] = new_bucket
                else:
                    del self._idle[key]

            total = sum(len(b) for b in self._idle.values())
            if total > POOL_MAX_TOTAL:
                all_conns = []
                for bucket in self._idle.values():
                    all_conns.extend(bucket)
                all_conns.sort(key=lambda x: x[1])
                excess = total - POOL_MAX_TOTAL
                for ws, _ in all_conns[:excess]:
                    to_close.append(ws)
                for key, bucket in self._idle.items():
                    new_bucket = deque([(w, c) for w, c in bucket if w not in to_close])
                    if new_bucket:
                        self._idle[key] = new_bucket
                    else:
                        del self._idle[key]

            for ws in to_close:
                asyncio.create_task(self._quiet_close(ws))

    async def close_all(self) -> None:
        async with self._lock:
            for bucket in self._idle.values():
                for ws, _ in bucket:
                    asyncio.create_task(self._quiet_close(ws))
            self._idle.clear()

    @staticmethod
    async def _quiet_close(ws: RawWebSocket) -> None:
        try:
            await ws.close()
        except Exception:
            pass


_ws_pool = WsConnectionPool()


async def _resolve(host: str) -> Optional[str]:
    try:
        _socket.inet_pton(_socket.AF_INET, host)
        return host
    except OSError:
        pass
    try:
        _socket.inet_pton(_socket.AF_INET6, host)
        return host
    except OSError:
        pass
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=_socket.AF_UNSPEC,
                             type=_socket.SOCK_STREAM, proto=0),
            timeout=DNS_TIMEOUT
        )
        if infos:
            for info in infos:
                if info[0] == _socket.AF_INET:
                    return info[4][0]
            return infos[0][4][0]
    except Exception:
        pass
    return None


def _reply(rep: int, bind_addr: str = '0.0.0.0', bind_port: int = 0) -> bytes:
    try:
        packed = _socket.inet_pton(_socket.AF_INET, bind_addr)
        atyp = _ATYP_IPV4
    except OSError:
        try:
            packed = _socket.inet_pton(_socket.AF_INET6, bind_addr)
            atyp = _ATYP_IPV6
        except OSError:
            packed = _socket.inet_pton(_socket.AF_INET, '0.0.0.0')
            atyp = _ATYP_IPV4
    return bytes([_VER, rep, 0x00, atyp]) + packed + bind_port.to_bytes(2, 'big')


async def _tcp_to_ws(reader: asyncio.StreamReader, ws: RawWebSocket, label: str,
                     stop_event: asyncio.Event, activity: List[float]) -> None:
    bytes_sent = 0
    close_reason = "normal"
    try:
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(reader.read(WS_MAX_FRAME_SIZE), timeout=IDLE_POLL)
                if not data:
                    close_reason = "tcp_eof"
                    break
                activity[0] = time.monotonic()
                offset = 0
                while offset < len(data):
                    if stop_event.is_set():
                        close_reason = "stopped"
                        break
                    chunk = data[offset:offset + WS_MAX_FRAME_SIZE]
                    await ws.send(chunk)
                    bytes_sent += len(chunk)
                    _stats.bytes_up += len(chunk)
                    offset += len(chunk)
            except asyncio.TimeoutError:
                # Tear down only if the WHOLE tunnel is idle, not just this
                # direction. A live download keeps the other direction busy.
                if time.monotonic() - activity[0] > WS_IDLE_TIMEOUT:
                    close_reason = f"idle_timeout({WS_IDLE_TIMEOUT:.0f}s)"
                    break
                continue
            except asyncio.IncompleteReadError:
                close_reason = "tcp_incomplete"
                break
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                close_reason = f"tcp_error({type(exc).__name__})"
                break
    except asyncio.CancelledError:
        close_reason = "cancelled"
    except Exception as exc:
        log.error("[%s] tcp->ws: %s", label, exc, exc_info=True)
        close_reason = f"unexpected({type(exc).__name__})"
    finally:
        log.debug("[%s] tcp->ws ended: %s (sent %d bytes)", label, close_reason, bytes_sent)
        stop_event.set()


async def _ws_to_tcp(ws: RawWebSocket, writer: asyncio.StreamWriter, label: str,
                     stop_event: asyncio.Event, activity: List[float]) -> None:
    bytes_recv = 0
    close_reason = "normal"
    try:
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=IDLE_POLL)
                if data is None:
                    close_reason = "ws_closed"
                    break
                activity[0] = time.monotonic()
                writer.write(data)
                try:
                    await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
                except asyncio.TimeoutError:
                    close_reason = "tcp_write_timeout(drain)"
                    break
                bytes_recv += len(data)
                _stats.bytes_down += len(data)
            except asyncio.TimeoutError:
                if time.monotonic() - activity[0] > WS_IDLE_TIMEOUT:
                    close_reason = f"idle_timeout({WS_IDLE_TIMEOUT:.0f}s)"
                    break
                continue
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                close_reason = f"ws_error({type(exc).__name__})"
                break
    except asyncio.CancelledError:
        close_reason = "cancelled"
    except Exception as exc:
        log.error("[%s] ws->tcp: %s", label, exc, exc_info=True)
        close_reason = f"unexpected({type(exc).__name__})"
    finally:
        log.debug("[%s] ws->tcp ended: %s (recv %d bytes)", label, close_reason, bytes_recv)
        stop_event.set()


async def _ws_pinger(ws: RawWebSocket, label: str,
                     stop_event: asyncio.Event, activity: List[float]) -> None:
    """Send an empty masked WS PING when the tunnel is quiet, so CF does not
    close an otherwise-healthy keep-alive connection. Built locally to avoid
    touching RawWebSocket internals; a single write() is atomic w.r.t. the
    other pump's send()."""
    try:
        while not stop_event.is_set():
            await asyncio.sleep(PING_INTERVAL)
            if stop_event.is_set():
                break
            if time.monotonic() - activity[0] < PING_INTERVAL:
                continue
            try:
                ws.writer.write(b'\x89\x80' + os.urandom(4))
                await ws.writer.drain()
            except Exception:
                break
    except asyncio.CancelledError:
        pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info('peername')
    label = f"{peer[0]}:{peer[1]}" if peer else '?'
    ws: Optional[RawWebSocket] = None
    target_ip = ''
    target_port = 0
    stop_event = asyncio.Event()
    slot_acquired = False
    counted_active = False
    ping_task: Optional[asyncio.Task] = None

    try:
        hdr = await asyncio.wait_for(reader.readexactly(2), timeout=HANDSHAKE_READ_TIMEOUT)
        if hdr[0] != _VER:
            return
        methods = await asyncio.wait_for(reader.readexactly(hdr[1]), timeout=HANDSHAKE_READ_TIMEOUT)

        if REQUIRE_AUTH:
            selected_method = _AUTH_PASSWORD if _AUTH_PASSWORD in methods else None
        else:
            selected_method = _AUTH_NONE if _AUTH_NONE in methods else None

        if selected_method is None:
            writer.write(bytes([_VER, _AUTH_REJECT]))
            await writer.drain()
            return

        writer.write(bytes([_VER, selected_method]))
        await writer.drain()

        if selected_method == _AUTH_PASSWORD:
            auth_ver = (await asyncio.wait_for(reader.readexactly(1), timeout=HANDSHAKE_READ_TIMEOUT))[0]
            if auth_ver != _AUTH_VER:
                return
            ulen = (await asyncio.wait_for(reader.readexactly(1), timeout=HANDSHAKE_READ_TIMEOUT))[0]
            uname = (await asyncio.wait_for(reader.readexactly(ulen), timeout=HANDSHAKE_READ_TIMEOUT)).decode('utf-8', errors='ignore')
            plen = (await asyncio.wait_for(reader.readexactly(1), timeout=HANDSHAKE_READ_TIMEOUT))[0]
            passwd = (await asyncio.wait_for(reader.readexactly(plen), timeout=HANDSHAKE_READ_TIMEOUT)).decode('utf-8', errors='ignore')
            if uname == SOCKS_USER and passwd == SOCKS_PASS:
                writer.write(bytes([_AUTH_VER, _AUTH_SUCCESS]))
                await writer.drain()
            else:
                writer.write(bytes([_AUTH_VER, _AUTH_FAILURE]))
                await writer.drain()
                log.warning("[%s] Auth failed for '%s'", label, uname)
                return

        req = await asyncio.wait_for(reader.readexactly(4), timeout=HANDSHAKE_READ_TIMEOUT)
        if req[1] != _CMD_CONNECT:
            writer.write(_reply(_REP_CMD_UNSUPPORTED))
            await writer.drain()
            return

        if req[3] == _ATYP_IPV4:
            target_host = _socket.inet_ntoa(await asyncio.wait_for(reader.readexactly(4), timeout=HANDSHAKE_READ_TIMEOUT))
        elif req[3] == _ATYP_DOMAIN:
            ln = (await asyncio.wait_for(reader.readexactly(1), timeout=HANDSHAKE_READ_TIMEOUT))[0]
            target_host = (await asyncio.wait_for(reader.readexactly(ln), timeout=HANDSHAKE_READ_TIMEOUT)).decode('ascii', errors='replace')
        elif req[3] == _ATYP_IPV6:
            target_host = _socket.inet_ntop(_socket.AF_INET6, await asyncio.wait_for(reader.readexactly(16), timeout=HANDSHAKE_READ_TIMEOUT))
        else:
            writer.write(_reply(_REP_ATYP_UNSUPPORTED))
            await writer.drain()
            return

        target_port = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), timeout=HANDSHAKE_READ_TIMEOUT), 'big')

        if target_port != 443:
            writer.write(_reply(_REP_PORT_RESTRICTED))
            await writer.drain()
            log.warning("[%s] Port %d rejected (only 443 supported)", label, target_port)
            return

        log.info("[%s] CONNECT %s:%d", label, target_host, target_port)

        target_ip = await _resolve(target_host)
        if target_ip is None:
            writer.write(_reply(_REP_HOST_UNREACH))
            await writer.drain()
            log.warning("[%s] Cannot resolve %s", label, target_host)
            return

        # Concurrency gate: brief queue, then fast-fail so the client's own
        # retry/backoff takes over instead of piling up worker connections.
        assert _conn_sem is not None
        try:
            await asyncio.wait_for(
                _conn_sem.acquire(),
                timeout=QUEUE_TIMEOUT if QUEUE_TIMEOUT > 0 else 0.0001,
            )
            slot_acquired = True
        except asyncio.TimeoutError:
            _stats.rejected_busy += 1
            writer.write(_reply(_REP_GENERAL_FAILURE))
            await writer.drain()
            log.warning("[%s] busy (%d/%d), rejected", label, _stats.active, MAX_CONNS)
            return

        ws = await _ws_pool.acquire(target_ip, target_port, WORKER_DOMAINS, CONNECT_BUDGET)
        if ws is None:
            writer.write(_reply(_REP_HOST_UNREACH))
            await writer.drain()
            log.warning("[%s] Cannot connect to %s:443 via WS", label, target_ip)
            return

        writer.write(_reply(_REP_OK))
        await writer.drain()

        _stats.total += 1
        _stats.active += 1
        counted_active = True
        activity = [time.monotonic()]

        t_up = asyncio.create_task(_tcp_to_ws(reader, ws, label, stop_event, activity))
        t_dn = asyncio.create_task(_ws_to_tcp(ws, writer, label, stop_event, activity))
        pumps = {t_up, t_dn}
        if PING_INTERVAL > 0:
            ping_task = asyncio.create_task(_ws_pinger(ws, label, stop_event, activity))

        try:
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            if ping_task is not None:
                ping_task.cancel()
                await asyncio.gather(ping_task, return_exceptions=True)

        log.info("[%s] Closed (%s:%d)", label, target_host, target_port)

    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        log.debug("[%s] Handshake error/disconnect", label)
    except Exception as exc:
        log.error("[%s] Unexpected: %s", label, exc, exc_info=True)
    finally:
        stop_event.set()
        if counted_active:
            _stats.active -= 1
        if slot_acquired and _conn_sem is not None:
            _conn_sem.release()
        if ws is not None:
            _ws_pool.release(ws, target_ip, target_port)
        try:
            writer.close()
            if sys.version_info >= (3, 12):
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
                except Exception:
                    pass
            else:
                await writer.wait_closed()
        except Exception:
            pass


async def _pool_cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(30.0)
            await _ws_pool.cleanup()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Pool cleanup error: %s", exc, exc_info=True)


async def _prewarm_loop() -> None:
    while True:
        try:
            await asyncio.sleep(PREWARM_INTERVAL)
            if _shutdown_event is not None and _shutdown_event.is_set():
                break
            await _ws_pool.prewarm()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Prewarm error: %s", exc, exc_info=True)


async def _stats_reporter() -> None:
    while True:
        try:
            await asyncio.sleep(60.0)
            log.info("stats: %s | idle_pool=%d | cooldown=%s",
                     _stats.summary(),
                     sum(len(b) for b in _ws_pool._idle.values()),
                     _ws_pool.cooldown_summary())
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Stats error: %s", exc, exc_info=True)


async def _run() -> None:
    global _shutdown_event, _conn_sem
    _shutdown_event = asyncio.Event()
    _conn_sem = asyncio.Semaphore(MAX_CONNS)

    if not WORKER_DOMAINS:
        log.error("CFPROXY_WORKER_DOMAIN not set")
        sys.exit(1)

    if BUF_KB > 0:
        proxy_config.buffer_size = max(4, BUF_KB) * 1024

    def _client_cb(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        if _shutdown_event is not None and _shutdown_event.is_set():
            w.close()
            return
        task = asyncio.create_task(_handle(r, w))
        _handler_tasks.add(task)
        task.add_done_callback(_handler_tasks.discard)

    server = await asyncio.start_server(_client_cb, SOCKS_HOST, SOCKS_PORT, reuse_address=True)
    for sock in server.sockets:
        try:
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            if hasattr(_socket, 'TCP_KEEPIDLE'):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE, 60)
            if hasattr(_socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL, 10)
            if hasattr(_socket, 'TCP_KEEPCNT'):
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT, 6)
        except (OSError, AttributeError):
            pass

    loop_tasks = [
        asyncio.create_task(_pool_cleanup_loop()),
        asyncio.create_task(_stats_reporter()),
    ]
    if PREWARM_PER_TARGET > 0:
        loop_tasks.append(asyncio.create_task(_prewarm_loop()))

    log.info("=" * 54)
    log.info(" SOCKS5 Proxy (WS via CF Worker)")
    log.info(" Listen  : %s:%d", SOCKS_HOST, SOCKS_PORT)
    log.info(" Auth    : %s", "ENABLED" if REQUIRE_AUTH else "DISABLED")
    log.info(" Workers : %s", ", ".join(WORKER_DOMAINS))
    log.info(" Note    : Only port 443 supported (Worker limitation)")
    log.info(" Conns   : max=%d queue=%.1fs connect_budget=%.1fs",
             MAX_CONNS, QUEUE_TIMEOUT, CONNECT_BUDGET)
    log.info(" Idle    : %.0fs (activity-based) ping=%.0fs",
             WS_IDLE_TIMEOUT, PING_INTERVAL)
    log.info(" Pool    : prewarm=%d/target max_age=%.0fs max_total=%d (pre-connect only)",
             PREWARM_PER_TARGET, POOL_MAX_AGE, POOL_MAX_TOTAL)
    log.info("=" * 54)

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        _shutdown_event.set()
        for task in loop_tasks:
            task.cancel()
        await asyncio.gather(*loop_tasks, return_exceptions=True)
        handlers = list(_handler_tasks)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await _ws_pool.close_all()


def main() -> None:
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    def _signal_handler(signum, frame):
        log.info("Received signal %d", signum)
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if loop is not None:
            loop.call_soon_threadsafe(sys.exit, 0)
        else:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        log.info("SOCKS5 stopped")
    except Exception as exc:
        log.critical("SOCKS5 crashed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
