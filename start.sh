#!/bin/sh
# Container entry point.
# Always runs the HTTP server (main_http.py). When GROUP_AGENT_ENABLED=true it also runs
# the WhatsApp bridge (wa_bridge/) in the background, restarting it if it ever stops.

PORT="${PORT:-3000}"
export PORT

case "$(echo "${GROUP_AGENT_ENABLED:-false}" | tr 'A-Z' 'a-z')" in
  1|true|yes|on)
    export AGENT_EVENTS_URL="${AGENT_EVENTS_URL:-http://127.0.0.1:${PORT}/group-agent/events}"
    export BRIDGE_AUTH_DIR="${BRIDGE_AUTH_DIR:-/data/wa_auth}"
    mkdir -p "$BRIDGE_AUTH_DIR"
    (
      while true; do
        node /app/wa_bridge/index.js
        echo "[start] WhatsApp bridge stopped - restarting in 5s" >&2
        sleep 5
      done
    ) &
    ;;
esac

exec python main_http.py
