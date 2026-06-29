#!/bin/sh
# Scheduler entrypoint for the JobMatch AI batch pipeline.
#
# Runs `python main.py` on a fixed interval so the whole thing lives inside
# Docker (no host cron / Task Scheduler needed). Controlled by env vars:
#
#   RUN_INTERVAL_SECONDS  Seconds between runs. Default 21600 (= 6 hours).
#                         Set to 0 to run once and exit (one-shot mode).
#   RUN_ON_START          "true" (default): run immediately on container start.
#                         "false": wait one interval before the first run.
#
# A failed run never breaks the schedule; the loop logs the exit code and keeps
# going. SIGTERM/SIGINT (docker stop / compose down) shuts down promptly.
set -eu

INTERVAL="${RUN_INTERVAL_SECONDS:-43200}"
RUN_ON_START="${RUN_ON_START:-true}"

# Validate INTERVAL is a non-negative integer; fall back to 6h otherwise.
case "$INTERVAL" in
  ''|*[!0-9]*)
    echo "[entrypoint] invalid RUN_INTERVAL_SECONDS='$INTERVAL'; using 43200 (6h)."
    INTERVAL=43200
    ;;
esac

stop() {
  echo "[entrypoint] stop signal received; shutting down."
  [ -n "${SLEEP_PID:-}" ] && kill "$SLEEP_PID" 2>/dev/null || true
  exit 0
}
trap stop TERM INT

run_pipeline() {
  echo "[entrypoint] $(date -u '+%Y-%m-%dT%H:%M:%SZ') starting pipeline run..."
  set +e
  python main.py
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    echo "[entrypoint] run finished OK."
  else
    echo "[entrypoint] run exited with code ${rc}; continuing schedule."
  fi
}

# One-shot mode.
if [ "$INTERVAL" -eq 0 ]; then
  run_pipeline
  exit 0
fi

echo "[entrypoint] scheduled mode: every ${INTERVAL}s (RUN_ON_START=${RUN_ON_START})."
first=1
while true; do
  if [ "$first" -eq 1 ] && [ "$RUN_ON_START" != "true" ]; then
    echo "[entrypoint] RUN_ON_START=false; waiting ${INTERVAL}s before the first run."
  else
    run_pipeline
  fi
  first=0
  echo "[entrypoint] sleeping ${INTERVAL}s until the next run..."
  # Background sleep + wait so a stop signal interrupts the wait immediately.
  sleep "$INTERVAL" &
  SLEEP_PID=$!
  wait "$SLEEP_PID"
done
