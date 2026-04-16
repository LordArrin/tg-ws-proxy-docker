#!/bin/bash
set -euo pipefail

ARGS=(
    --host "${PROXY_HOST:-0.0.0.0}"
    --port "${PROXY_PORT:-1443}"
    --buf-kb "${PROXY_BUF:-512}"
    --pool-size "${PROXY_POOL_SIZE:-8}"
)

if [[ -n "${PROXY_SECRET:-}" ]]; then
    ARGS+=(--secret "$PROXY_SECRET")
fi

for dc in ${PROXY_DC_IPS}; do
    ARGS+=(--dc-ip "$dc")
done

exec /opt/venv/bin/python -u proxy/tg_ws_proxy.py "${ARGS[@]}"