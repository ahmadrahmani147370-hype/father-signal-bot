#!/usr/bin/env bash
# Start FATHER SIGNAL BOT with a wake-lock and automatic restart after crashes / network loss.
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null && termux-wake-lock
trap 'command -v termux-wake-unlock >/dev/null && termux-wake-unlock; exit 0' INT TERM
while true; do
  python main.py
  code=$?
  [ "$code" -eq 2 ] && { echo "Config error (see above). Fix .env and run again."; break; }
  echo "Bot stopped (exit $code). Restarting in 15s... (Ctrl+C to quit)"
  sleep 15
done
command -v termux-wake-unlock >/dev/null && termux-wake-unlock
