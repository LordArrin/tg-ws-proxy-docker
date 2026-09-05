#!/usr/bin/env python3

import asyncio
import os
import random
import sys
import signal
import logging
import time
from typing import Dict, List

if __name__ == '__main__' and (__package__ is None or __package__ == ''):
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    __package__ = 'proxy'

from .raw_websocket import RawWebSocket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-5s [KeepAlive] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('keepalive')

PING_INTERVAL_MIN = 45.0
PING_INTERVAL_MAX = 120.0
PING_TIMEOUT = 5.0
CONNECT_TIMEOUT = 8.0
MAX_FAILURES = 5
COOLDOWN_BASE = 60.0
COOLDOWN_MAX = 3600.0

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

_domain_failures: Dict[str, int] = {}
_domain_cooldown_until: Dict[str, float] = {}

def _get_available_domains() -> List[str]:
    now = time.monotonic()
    available = []
    for domain in WORKER_DOMAINS:
        cooldown_until = _domain_cooldown_until.get(domain, 0)
        if now >= cooldown_until:
            available.append(domain)
            if cooldown_until > 0:
                _domain_cooldown_until.pop(domain, None)
    return available

def _record_failure(domain: str) -> None:
    failures = _domain_failures.get(domain, 0) + 1
    _domain_failures[domain] = failures
    if failures >= MAX_FAILURES:
        cooldown = min(COOLDOWN_BASE * (2 ** (failures - MAX_FAILURES)), COOLDOWN_MAX)
        _domain_cooldown_until[domain] = time.monotonic() + cooldown
        log.warning("Domain %s failed %d times, cooldown %.0fs", domain, failures, cooldown)

def _record_success(domain: str) -> None:
    _domain_failures.pop(domain, None)
    _domain_cooldown_until.pop(domain, None)

async def _ping_domain(domain: str) -> bool:
    path = "/apiws?dst=127.0.0.1"
    try:
        ws = await asyncio.wait_for(
            RawWebSocket.connect(domain, domain, timeout=CONNECT_TIMEOUT, path=path),
            timeout=CONNECT_TIMEOUT + 2.0
        )
        try:
            await asyncio.wait_for(ws.send(b'\x00'), timeout=PING_TIMEOUT)
            try:
                await asyncio.wait_for(ws.recv(), timeout=PING_TIMEOUT)
            except asyncio.TimeoutError:
                pass
        except Exception:
            pass
        try:
            await asyncio.wait_for(ws.close(), timeout=2.0)
        except Exception:
            pass
        return True
    except Exception:
        return False

async def _keepalive_loop() -> None:
    log.info("Agent started")
    log.info("Domains: %s", ", ".join(WORKER_DOMAINS))
    log.info("Interval: %.0f-%.0fs", PING_INTERVAL_MIN, PING_INTERVAL_MAX)

    while True:
        available = _get_available_domains()
        if not available:
            log.warning("No available domains (all in cooldown)")
            await asyncio.sleep(10.0)
            continue

        domain = random.choice(available)
        if await _ping_domain(domain):
            _record_success(domain)
            log.info("OK %s", domain)
        else:
            _record_failure(domain)
            log.warning("FAIL %s", domain)

        await asyncio.sleep(random.uniform(PING_INTERVAL_MIN, PING_INTERVAL_MAX))

async def _stats_reporter() -> None:
    while True:
        await asyncio.sleep(60.0)
        now = time.monotonic()
        parts = []
        for domain in WORKER_DOMAINS:
            failures = _domain_failures.get(domain, 0)
            cooldown_until = _domain_cooldown_until.get(domain, 0)
            if failures > 0:
                parts.append(f"{domain}:{failures}F")
            if cooldown_until > now:
                parts.append(f"{domain}:{cooldown_until - now:.0f}s")
        if parts:
            log.info("Stats: %s", ", ".join(parts))

async def _main() -> None:
    if not WORKER_DOMAINS:
        log.error("CFPROXY_WORKER_DOMAIN not set")
        return

    tasks = [
        asyncio.create_task(_keepalive_loop()),
        asyncio.create_task(_stats_reporter())
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

def main() -> None:
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    def signal_handler(signum, frame):
        log.info("Received signal %d", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Agent stopped")
    except Exception as exc:
        log.critical("Agent crashed: %s", exc, exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    main()