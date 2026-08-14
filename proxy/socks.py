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

from .raw_websocket import RawWebSocket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-5s [SOCKS5] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('socks5')

SOCKS_HOST = os.environ.get('SOCKS_HOST', '0.0.0.0')
SOCKS_PORT = int(os.environ.get('SOCKS_PORT', '1080'))
CONNECT_TIMEOUT = 15.0
DNS_TIMEOUT = 8.0
WS_MAX_FRAME_SIZE = 64 * 1024
WS_IDLE_TIMEOUT = 180.0
MAX_RETRIES = 5
POOL_MAX_AGE = 90.0
POOL_MAX_TOTAL = 8
POOL_SIZE_PER_TARGET = 2


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
_REP_OK, _REP_HOST_UNREACH, _REP_CMD_UNSUPPORTED, _REP_ATYP_UNSUPPORTED = 0x00, 0x04, 0x07, 0x08
_REP_PORT_RESTRICTED = 0x02
_AUTH_VER, _AUTH_SUCCESS, _AUTH_FAILURE = 0x01, 0x00, 0xFF

_active_connections: Set[asyncio.Task] = set()
_connection_count = 0
_shutdown_event: Optional[asyncio.Event] = None


class WsConnectionPool:
    def __init__(self):
        self._idle: Dict[Tuple[str, int], Deque[Tuple[RawWebSocket, float]]] = {}
        self._lock = asyncio.Lock()

    def _total_connections(self) -> int:
        return sum(len(bucket) for bucket in self._idle.values())

    async def acquire(self, target_ip: str, target_port: int, domains: List[str]) -> Optional[RawWebSocket]:
        key = (target_ip, target_port)
        async with self._lock:
            bucket = self._idle.get(key)
            if bucket:
                while bucket:
                    ws, created = bucket.popleft()
                    if time.monotonic() - created > POOL_MAX_AGE:
                        asyncio.create_task(self._quiet_close(ws))
                        continue
                    if ws._closed:
                        continue
                    if ws.writer.transport.is_closing():
                        asyncio.create_task(self._quiet_close(ws))
                        continue
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        if data is None:
                            continue
                        asyncio.create_task(self._quiet_close(ws))
                        continue
                    except asyncio.TimeoutError:
                        log.debug("Pool hit for %s:%d", target_ip, target_port)
                        return ws
                    except Exception:
                        continue

        return await self._create_new(target_ip, target_port, domains)

    async def _create_new(self, target_ip: str, target_port: int, domains: List[str]) -> Optional[RawWebSocket]:
        global _connection_count
        for attempt in range(MAX_RETRIES):
            doms = list(domains)
            random.shuffle(doms)
            for domain in doms:
                if _shutdown_event and _shutdown_event.is_set():
                    return None
                path = f"/apiws?{urlencode({'dst': target_ip})}"
                try:
                    ws = await asyncio.wait_for(
                        RawWebSocket.connect(domain, domain, timeout=CONNECT_TIMEOUT, path=path),
                        timeout=CONNECT_TIMEOUT + 2.0
                    )
                    _connection_count += 1
                    log.debug("Pool miss for %s:%d, created via %s (#%d)",
                              target_ip, target_port, domain, _connection_count)
                    return ws
                except asyncio.TimeoutError:
                    log.debug("WS timeout via %s (%d/%d)", domain, attempt + 1, MAX_RETRIES)
                except Exception as exc:
                    log.debug("WS fail via %s: %s (%d/%d)", domain, repr(exc), attempt + 1, MAX_RETRIES)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(min(2.0 ** attempt, 10.0))
        return None

    def release(self, ws: Optional[RawWebSocket], target_ip: str, target_port: int) -> None:
        if ws is None or ws._closed:
            return
        try:
            if ws.writer.transport.is_closing():
                return
        except Exception:
            return
        
        key = (target_ip, target_port)
        bucket = self._idle.setdefault(key, deque())
        
        if len(bucket) >= POOL_SIZE_PER_TARGET or self._total_connections() >= POOL_MAX_TOTAL:
            asyncio.create_task(self._quiet_close(ws))
            return
        
        bucket.append((ws, time.monotonic()))
        log.debug("Released WS to pool for %s:%d (pool: %d/%d)",
                  target_ip, target_port, self._total_connections(), POOL_MAX_TOTAL)

    async def cleanup(self):
        async with self._lock:
            now = time.monotonic()
            to_close = []
            
            for key in list(self._idle.keys()):
                bucket = self._idle[key]
                new_bucket: Deque[Tuple[RawWebSocket, float]] = deque()
                while bucket:
                    ws, created = bucket.popleft()
                    age = now - created
                    if age > POOL_MAX_AGE or ws._closed:
                        to_close.append(ws)
                    else:
                        new_bucket.append((ws, created))
                
                if new_bucket:
                    self._idle[key] = new_bucket
                else:
                    del self._idle[key]
            
            if self._total_connections() > POOL_MAX_TOTAL:
                all_conns = []
                for bucket in self._idle.values():
                    all_conns.extend(bucket)
                all_conns.sort(key=lambda x: x[1])
                
                excess = self._total_connections() - POOL_MAX_TOTAL
                for i in range(min(excess, len(all_conns))):
                    ws, created = all_conns[i]
                    to_close.append(ws)
                    
                    for key, bucket in self._idle.items():
                        new_bucket = deque([(w, c) for w, c in bucket if w != ws])
                        if new_bucket:
                            self._idle[key] = new_bucket
                        else:
                            del self._idle[key]
            
            for ws in to_close:
                asyncio.create_task(self._quiet_close(ws))

    @staticmethod
    async def _quiet_close(ws: RawWebSocket):
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
            loop.getaddrinfo(host, None, family=_socket.AF_UNSPEC, type=_socket.SOCK_STREAM, proto=0),
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


async def _tcp_to_ws(reader: asyncio.StreamReader, ws: RawWebSocket, label: str, stop_event: asyncio.Event) -> None:
    bytes_sent = 0
    chunks_sent = 0
    close_reason = "normal"
    try:
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(reader.read(WS_MAX_FRAME_SIZE), timeout=WS_IDLE_TIMEOUT)
                if not data:
                    close_reason = "tcp_eof"
                    break
                offset = 0
                while offset < len(data):
                    if stop_event.is_set():
                        close_reason = "stopped"
                        break
                    chunk = data[offset:offset + WS_MAX_FRAME_SIZE]
                    await ws.send(chunk)
                    bytes_sent += len(chunk)
                    chunks_sent += 1
                    offset += len(chunk)
            except asyncio.TimeoutError:
                close_reason = f"tcp_read_timeout({WS_IDLE_TIMEOUT}s)"
                break
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
        log.debug("[%s] tcp->ws ended: %s (sent %d bytes, %d chunks)", label, close_reason, bytes_sent, chunks_sent)
        stop_event.set()


async def _ws_to_tcp(ws: RawWebSocket, writer: asyncio.StreamWriter, label: str, stop_event: asyncio.Event) -> None:
    bytes_recv = 0
    chunks_recv = 0
    last_activity = time.monotonic()
    close_reason = "normal"
    try:
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=WS_IDLE_TIMEOUT)
                if data is None:
                    close_reason = "ws_closed"
                    break
                writer.write(data)
                try:
                    await asyncio.wait_for(writer.drain(), timeout=30.0)
                except asyncio.TimeoutError:
                    close_reason = "tcp_write_timeout(drain)"
                    break
                bytes_recv += len(data)
                chunks_recv += 1
                last_activity = time.monotonic()
            except asyncio.TimeoutError:
                if time.monotonic() - last_activity > WS_IDLE_TIMEOUT * 2:
                    close_reason = f"ws_idle_timeout({WS_IDLE_TIMEOUT}s)"
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
        log.debug("[%s] ws->tcp ended: %s (recv %d bytes, %d chunks)", label, close_reason, bytes_recv, chunks_recv)
        stop_event.set()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info('peername')
    label = f"{peer[0]}:{peer[1]}" if peer else '?'
    ws: Optional[RawWebSocket] = None
    target_ip = ''
    target_port = 0
    stop_event = asyncio.Event()
    tasks: Set[asyncio.Task] = set()
    should_release_to_pool = False

    try:
        hdr = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        if hdr[0] != _VER:
            return
        methods = await asyncio.wait_for(reader.readexactly(hdr[1]), timeout=10)

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
            auth_ver = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            if auth_ver != _AUTH_VER:
                return
            ulen = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            uname = (await asyncio.wait_for(reader.readexactly(ulen), timeout=10)).decode('utf-8', errors='ignore')
            plen = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            passwd = (await asyncio.wait_for(reader.readexactly(plen), timeout=10)).decode('utf-8', errors='ignore')
            if uname == SOCKS_USER and passwd == SOCKS_PASS:
                writer.write(bytes([_AUTH_VER, _AUTH_SUCCESS]))
                await writer.drain()
            else:
                writer.write(bytes([_AUTH_VER, _AUTH_FAILURE]))
                await writer.drain()
                log.warning("[%s] Auth failed for '%s'", label, uname)
                return

        req = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        if req[1] != _CMD_CONNECT:
            writer.write(_reply(_REP_CMD_UNSUPPORTED))
            await writer.drain()
            return

        if req[3] == _ATYP_IPV4:
            target_host = _socket.inet_ntoa(await asyncio.wait_for(reader.readexactly(4), timeout=10))
        elif req[3] == _ATYP_DOMAIN:
            ln = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            target_host = (await asyncio.wait_for(reader.readexactly(ln), timeout=10)).decode('ascii', errors='replace')
        elif req[3] == _ATYP_IPV6:
            target_host = _socket.inet_ntop(_socket.AF_INET6, await asyncio.wait_for(reader.readexactly(16), timeout=10))
        else:
            writer.write(_reply(_REP_ATYP_UNSUPPORTED))
            await writer.drain()
            return

        target_port = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), timeout=10), 'big')

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

        ws = await _ws_pool.acquire(target_ip, target_port, WORKER_DOMAINS)
        if ws is None:
            writer.write(_reply(_REP_HOST_UNREACH))
            await writer.drain()
            log.warning("[%s] Cannot connect to %s:443 via WS", label, target_ip)
            return

        writer.write(_reply(_REP_OK))
        await writer.drain()

        t_up = asyncio.create_task(_tcp_to_ws(reader, ws, label, stop_event))
        t_dn = asyncio.create_task(_ws_to_tcp(ws, writer, label, stop_event))
        tasks = {t_up, t_dn}
        _active_connections.update(tasks)

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
                try:
                    await t
                except Exception:
                    pass
        finally:
            _active_connections.difference_update(tasks)

        log.info("[%s] Closed (%s:%d)", label, target_host, target_port)
        should_release_to_pool = True

    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        log.debug("[%s] Handshake error/disconnect", label)
    except Exception as exc:
        log.error("[%s] Unexpected: %s", label, exc, exc_info=True)
    finally:
        stop_event.set()
        if ws:
            if should_release_to_pool:
                _ws_pool.release(ws, target_ip, target_port)
            else:
                try:
                    await asyncio.wait_for(ws.close(), timeout=5.0)
                except Exception:
                    pass
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


async def _pool_cleanup_loop():
    while True:
        try:
            await asyncio.sleep(30.0)
            await _ws_pool.cleanup()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Pool cleanup error: %s", exc, exc_info=True)


async def _run() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    if not WORKER_DOMAINS:
        log.error("CFPROXY_WORKER_DOMAIN not set")
        sys.exit(1)

    server = await asyncio.start_server(_handle, SOCKS_HOST, SOCKS_PORT, reuse_address=True)
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

    cleanup_task = asyncio.create_task(_pool_cleanup_loop())

    log.info("=" * 54)
    log.info(" SOCKS5 Proxy (WS via CF Worker)")
    log.info(" Listen  : %s:%d", SOCKS_HOST, SOCKS_PORT)
    log.info(" Auth    : %s", "ENABLED" if REQUIRE_AUTH else "DISABLED")
    log.info(" Workers : %s", ", ".join(WORKER_DOMAINS))
    log.info(" Note    : Only port 443 supported (Worker limitation)")
    log.info(" Pool    : max_age=%.0fs, max_total=%d, per_target=%d",
             POOL_MAX_AGE, POOL_MAX_TOTAL, POOL_SIZE_PER_TARGET)
    log.info("=" * 54)

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        _shutdown_event.set()
        cleanup_task.cancel()
        try:
            await cleanup_task
        except Exception:
            pass
        if _active_connections:
            for task in list(_active_connections):
                task.cancel()
            await asyncio.gather(*_active_connections, return_exceptions=True)


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
