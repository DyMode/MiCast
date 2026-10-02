#!/bin/sh
set -eu

action="${1:-}"
case "$action" in
  start) route="start" ;;
  stop) route="session/stop" ;;
  volume)
    route="session/volume"
    case "${2:-}" in ''|*[!0-9.-]*) exit 2 ;; esac
    ;;
  *) exit 2 ;;
esac

# A shared event sequence gives retried requests their original ordering.
# Hold the lock only for allocation, never across an HTTP request.
callback_lock=/run/micast-callback-lock
callback_counter=/run/micast-callback-sequence
attempt=0
until mkdir "$callback_lock" 2>/dev/null; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 200 ] || exit 1
  sleep 0.01
done
trap 'rmdir "$callback_lock" 2>/dev/null || true' EXIT INT TERM
sequence=0
[ ! -f "$callback_counter" ] || sequence="$(cat "$callback_counter")"
sequence=$((sequence + 1))
printf '%s\n' "$sequence" > "$callback_counter"
rmdir "$callback_lock"
trap - EXIT INT TERM

payload="{\"device_id\":\"${MICAST_DEVICE_ID}\",\"token\":\"${MICAST_CALLBACK_TOKEN}\",\"epoch\":\"${MICAST_RECEIVER_EPOCH:-}\",\"event_seq\":$sequence"
[ "$action" != volume ] || payload="$payload,\"db\":$2"
payload="$payload}"
curl -fsS --connect-timeout 2 --max-time 3 --retry 2 --retry-delay 1 --retry-connrefused \
  -X POST "${MICAST_CALLBACK_BASE}/app/micast/api/playback/$route" \
  -H "Content-Type: application/json" --data "$payload" \
  >/dev/null || { echo "MiCast $action callback failed" >&2; exit 1; }
