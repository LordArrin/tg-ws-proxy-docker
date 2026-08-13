#!/bin/sh

set -eu

set -- \
    --host "${PROXY_HOST:-0.0.0.0}" \
    --port "${PROXY_PORT:-1443}" \
    --buf-kb "${PROXY_BUF:-4096}" \
    --pool-size "${PROXY_POOL_SIZE:-2}"

if [ -n "${PROXY_SECRET:-}" ]; then
    set -- "$@" --secret "$PROXY_SECRET"
fi

for dc in ${PROXY_DC_IPS:-}; do
    set -- "$@" --dc-ip "$dc"
done

if [ "${NO_CFPROXY:-}" = "true" ]; then
    set -- "$@" --no-cfproxy
else
    if [ -n "${CFPROXY_DOMAIN:-}" ]; then
        set -- "$@" --cfproxy-domain "$CFPROXY_DOMAIN"
    fi
    if [ -n "${CFPROXY_WORKER_DOMAIN:-}" ]; then
        set -- "$@" --cfproxy-worker-domain "$CFPROXY_WORKER_DOMAIN"
    fi
fi

if [ "${SOCKS_ENABLED:-false}" = "true" ] && [ -n "${CFPROXY_WORKER_DOMAIN:-}" ]; then
    echo "[Entrypoint] Starting SOCKS5 proxy on port ${SOCKS_PORT:-1080}..."
    /opt/venv/bin/python -u proxy/socks.py &
fi

if [ "${KEEPALIVE:-false}" = "true" ] && [ -n "${CFPROXY_WORKER_DOMAIN:-}" ]; then
    echo "[Entrypoint] Starting keepalive agent..."
    /opt/venv/bin/python -u proxy/keepalive.py &
fi

echo "[Entrypoint] Starting MTProto proxy..."
exec /opt/venv/bin/python -u proxy/tg_ws_proxy.py "$@"