#!/bin/bash
set -euo pipefail

ARGS=(
    --host "${PROXY_HOST:-0.0.0.0}"
    --port "${PROXY_PORT:-1443}"
    --buf-kb "${PROXY_BUF:-1024}"
    --pool-size "${PROXY_POOL_SIZE:-8}"
)

if [[ -n "${PROXY_SECRET:-}" ]]; then
    ARGS+=(--secret "$PROXY_SECRET")
fi

for dc in ${PROXY_DC_IPS}; do
    ARGS+=(--dc-ip "$dc")
done

if [[ "${NO_CFPROXY:-}" == "true" ]]; then
    ARGS+=(--no-cfproxy)
else
    if [[ -n "${CFPROXY_DOMAIN:-}" ]]; then
        ARGS+=(--cfproxy-domain "$CFPROXY_DOMAIN")
    fi
    if [[ -n "${CFPROXY_WORKER_DOMAIN:-}" ]]; then
        ARGS+=(--cfproxy-worker-domain "$CFPROXY_WORKER_DOMAIN")
    fi
fi

exec /opt/venv/bin/python -u proxy/tg_ws_proxy.py "${ARGS[@]}"