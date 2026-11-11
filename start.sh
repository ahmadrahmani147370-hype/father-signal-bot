#!/usr/bin/env bash
# Start FATHER SIGNAL BOT with wake-lock, auto-restart after crashes, and a heartbeat watchdog.
# The bot writes logs/heartbeat every minute. If it is older than STALE_SEC on two checks in a row,
# the bot is considered hung and is restarted.
cd "$(dirname "$0")"
mkdir -p logs
STALE_SEC=${STALE_SEC:-300}
PY=${PYTHON:-$(command -v python || command -v python3)}
command -v termux-wake-lock >/dev/null && termux-wake-lock
PID=""
cleanup() {
  [ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null
  command -v termux-wake-unlock >/dev/null && termux-wake-unlock
  exit 0
}
trap cleanup INT TERM

while true; do
  if [ -f logs/father_stdout.log ] && [ "$(wc -c < logs/father_stdout.log)" -gt 5000000 ]; then
    mv logs/father_stdout.log logs/father_stdout.log.1
  fi
  date +%s > logs/heartbeat
  "$PY" main.py >> logs/father_stdout.log 2>&1 &
  PID=$!
  echo "$(date '+%F %T') started bot pid $PID"
  strikes=0
  while kill -0 "$PID" 2>/dev/null; do
    sleep 60
    hb=$(cat logs/heartbeat 2>/dev/null || echo 0)
    age=$(( $(date +%s) - hb ))
    if [ "$age" -gt "$STALE_SEC" ]; then
      strikes=$((strikes + 1))
      echo "$(date '+%F %T') heartbeat stale (${age}s), strike $strikes"
      if [ "$strikes" -ge 2 ]; then
        echo "$(date '+%F %T') watchdog: bot looks hung - dumping threads and restarting"
        kill -USR1 "$PID" 2>/dev/null; sleep 2
        kill -TERM "$PID" 2>/dev/null; sleep 20
        kill -KILL "$PID" 2>/dev/null
        break
      fi
    else
      strikes=0
    fi
  done
  wait "$PID" 2>/dev/null
  code=$?
  if [ "$code" -eq 2 ]; then echo "Config error (see logs/father_stdout.log). Fix .env and run again."; break; fi
  echo "$(date '+%F %T') bot stopped (exit $code). Restarting in 15s... (Ctrl+C to quit)"
  sleep 15
done
command -v termux-wake-unlock >/dev/null && termux-wake-unlock
