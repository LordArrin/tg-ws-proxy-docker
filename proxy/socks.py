#!/usr/bin/env python3

import os
import sys
import signal
import asyncio
import logging
import socket as _socket
import random
from typing import Optional
from urllib.parse import urlencode

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

def _get_env_int(name: str, default: int) -> int:
    try: return int(os.environ.get(name, default))
    except ValueError: return default

def _get_env_float(name: str, default: float) -> float:
    try: return float(os.environ.get(name, default))
    except ValueError: return default

SOCKS_HOST = os.environ.get('SOCKS_HOST', '0.0.0.0')
SOCKS_PORT = _get_env_int('SOCKS_PORT', 1080)
CONNECT_TIMEOUT = _get_env_float('SOCKS_CONNECT_TIMEOUT', 10.0)
DNS_TIMEOUT = _get_env_float('SOCKS_DNS_TIMEOUT', 5.0)
WORKER_DOMAINS = [d.strip() for d in os.environ.get('CFPROXY_WORKER_DOMAIN', '').split() if d.strip()]

SOCKS_USER = os.environ.get('SOCKS_USER', '')
SOCKS_PASS = os.environ.get('SOCKS_PASS', '')
REQUIRE_AUTH = bool(SOCKS_USER and SOCKS_PASS)

_VER, _AUTH_NONE, _AUTH_PASSWORD, _AUTH_REJECT, _CMD_CONNECT = 0x05, 0x00, 0x02, 0xFF, 0x01
_ATYP_IPV4, _ATYP_DOMAIN, _ATYP_IPV6 = 0x01, 0x03, 0x04
_REP_OK, _REP_HOST_UNREACH, _REP_CMD_UNSUPPORTED, _REP_ATYP_UNSUPPORTED = 0x00, 0x04, 0x07, 0x08
_AUTH_VER, _AUTH_SUCCESS, _AUTH_FAILURE = 0x01, 0x00, 0xFF

async def _resolve(host: str, port: int) -> str:
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, family=_socket.AF_UNSPEC, type=_socket.SOCK_STREAM), timeout=DNS_TIMEOUT)
        if infos: return infos[0][4][0]
    except Exception: pass
    return host

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

async def _tcp_to_ws(reader: asyncio.StreamReader, ws: RawWebSocket, label: str) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data: break
            await ws.send(data)
    except (asyncio.CancelledError, ConnectionError, OSError, Exception): pass

async def _ws_to_tcp(ws: RawWebSocket, writer: asyncio.StreamWriter, label: str) -> None:
    try:
        while True:
            data = await ws.recv()
            if data is None: break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError, OSError, asyncio.IncompleteReadError, Exception): pass

async def _ws_connect(target_ip: str, target_port: int, label: str) -> Optional[RawWebSocket]:
    domains = list(WORKER_DOMAINS)
    random.shuffle(domains)
    
    for domain in domains:
        path = f"/apiws?{urlencode({'dst': target_ip, 'port': str(target_port)})}"
        try:
            ws = await RawWebSocket.connect(domain, domain, timeout=CONNECT_TIMEOUT, path=path)
            log.info("[%s] WS tunnel via %s -> %s:%d", label, domain, target_ip, target_port)
            return ws
        except Exception as exc:
            log.warning("[%s] WS %s failed: %s", label, domain, repr(exc))
    return None

async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info('peername')
    label = f"{peer[0]}:{peer[1]}" if peer else '?'
    ws = None
    try:
        hdr = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        if hdr[0] != _VER: return
        
        methods = await asyncio.wait_for(reader.readexactly(hdr[1]), timeout=10)
        
        if REQUIRE_AUTH:
            if _AUTH_PASSWORD in methods:
                selected_method = _AUTH_PASSWORD
            else:
                writer.write(bytes([_VER, _AUTH_REJECT]))
                await writer.drain()
                return
        else:
            if _AUTH_NONE in methods:
                selected_method = _AUTH_NONE
            else:
                writer.write(bytes([_VER, _AUTH_REJECT]))
                await writer.drain()
                return
                
        writer.write(bytes([_VER, selected_method]))
        await writer.drain()

        if selected_method == _AUTH_PASSWORD:
            auth_ver = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            if auth_ver != _AUTH_VER: return
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
            target_host = (await asyncio.wait_for(reader.readexactly(ln), timeout=10)).decode('ascii')
        elif req[3] == _ATYP_IPV6:
            target_host = _socket.inet_ntop(_socket.AF_INET6, await asyncio.wait_for(reader.readexactly(16), timeout=10))
        else:
            writer.write(_reply(_REP_ATYP_UNSUPPORTED))
            await writer.drain()
            return

        target_port = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), timeout=10), 'big')
        log.info("[%s] CONNECT %s:%d", label, target_host, target_port)

        target_ip = await _resolve(target_host, target_port)
        ws = await _ws_connect(target_ip, target_port, label)
        
        if ws is None:
            writer.write(_reply(_REP_HOST_UNREACH))
            await writer.drain()
            return

        writer.write(_reply(_REP_OK))
        await writer.drain()

        t_up = asyncio.create_task(_tcp_to_ws(reader, ws, label))
        t_dn = asyncio.create_task(_ws_to_tcp(ws, writer, label))
        done, pending = await asyncio.wait([t_up, t_dn], return_when=asyncio.FIRST_COMPLETED)
        
        for t in pending:
            t.cancel()
            try: await t
            except (asyncio.CancelledError, Exception): pass

        log.info("[%s] session closed (%s:%d)", label, target_host, target_port)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        log.debug("[%s] client disconnected or timeout", label)
    except Exception as exc:
        log.error("[%s] unexpected: %s", label, exc, exc_info=True)
    finally:
        if ws:
            try: await ws.close()
            except Exception: pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception: pass

async def _run() -> None:
    if not WORKER_DOMAINS:
        log.error("CFPROXY_WORKER_DOMAIN is not set. SOCKS5 proxy cannot start.")
        sys.exit(1)
        
    server = await asyncio.start_server(_handle, SOCKS_HOST, SOCKS_PORT)
    for sock in server.sockets:
        try: sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except (OSError, AttributeError): pass
        
    auth_status = "ENABLED" if REQUIRE_AUTH else "DISABLED"
    log.info("=" * 54)
    log.info(" SOCKS5 Proxy (WS tunnel via CF Worker)")
    log.info(" Listen   : %s:%d", SOCKS_HOST, SOCKS_PORT)
    log.info(" Auth     : %s", auth_status)
    log.info(" Workers  : %s (Load balanced)", ", ".join(WORKER_DOMAINS))
    log.info(" Timeout  : connect=%.0fs  dns=%.0fs", CONNECT_TIMEOUT, DNS_TIMEOUT)
    log.info("=" * 54)
    
    async with server:
        await server.serve_forever()

def main() -> None:
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try: asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit): log.info("SOCKS5 proxy stopped.")

if __name__ == '__main__':
    main()