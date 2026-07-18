import asyncio
import os
import random
import logging
import websockets

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-5s  [KeepAlive] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

async def ws_keepalive_worker():
    raw_domains = os.environ.get("CFPROXY_WORKER_DOMAIN", "")
    domains = [d.strip() for d in raw_domains.split() if d.strip()]
    
    if not domains:
        logger.info("CFPROXY_WORKER_DOMAIN is not set. Keepalive agent is shutting down.")
        return

    logger.info(f"Agent started for domains: {domains}")

    while True:
        domain = random.choice(domains)
        uri = f"wss://{domain}/apiws?dst=149.154.167.220"
        
        try:
            async with websockets.connect(
                uri, 
                open_timeout=5, 
                close_timeout=2,
                ping_interval=None,
                ping_timeout=None,
                additional_headers={"User-Agent": "KeepAlive-Agent/1.0"}
            ) as ws:
                await asyncio.sleep(1.0)
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=3.0)
                logger.info(f"Successful PING/PONG for {domain}")
                
        except Exception as e:
            logger.warning(f"Ping failure for {domain}: {type(e).__name__} - {e}")
            
        sleep_time = random.uniform(30.0, 90.0)
        await asyncio.sleep(sleep_time)

if __name__ == "__main__":
    try:
        asyncio.run(ws_keepalive_worker())
    except KeyboardInterrupt:
        logger.info("Agent stopped.")