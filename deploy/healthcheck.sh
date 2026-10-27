#!/usr/bin/env bash
# Alert if the daemon has stopped ticking during market hours.
# cron (every 5 minutes, weekdays):
#   */5 9-15 * * 1-5  /opt/swing_trader/deploy/healthcheck.sh shadow
set -u
MODE="${1:-shadow}"
DIR="${SWING_HOME:-/opt/swing_trader}"
HB="$DIR/state/$MODE/heartbeat.json"
MAX_AGE="${MAX_AGE:-300}"

[ -f /etc/swingtrader.env ] && . /etc/swingtrader.env

alert() {
  echo "swingtrader healthcheck: $1" >&2
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -fsS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=swingtrader CRITICAL: $1" >/dev/null || true
  fi
}

if [ ! -f "$HB" ]; then alert "no heartbeat file at $HB"; exit 2; fi
AGE=$(python3 -c "import json,time;print(int(time.time()-json.load(open('$HB'))['wall']))" 2>/dev/null || echo 999999)
if [ "$AGE" -gt "$MAX_AGE" ]; then
  alert "heartbeat is ${AGE}s old (limit ${MAX_AGE}s) - the daemon is not running. Broker-side GTT stops still protect open positions; entries and the watchdog are paused."
  exit 1
fi
exit 0
