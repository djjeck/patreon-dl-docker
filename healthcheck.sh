#!/bin/bash
# Default container healthcheck (baked into the image via HEALTHCHECK, so it applies
# automatically to any container started from this image — no compose change needed).
#
# Reports unhealthy in two cases, logging the reason so flaky state is diagnosable:
#   1. An auth hold is active — the configured cookie was rejected by Patreon and
#      downloads are paused until the user re-exports a fresh cookie. This is the signal
#      to refresh the cookie. (The container deliberately stays up so the archive UI
#      keeps serving; only its health goes red.)
#   2. The archive server has stopped responding on :3000 — but only when the server is
#      expected to be running. The entrypoint writes SERVER_MARKER when (and only when)
#      it starts patreon-dl-server, so this check is skipped in the no-DB / scheduler-only
#      startup mode where there is intentionally no server.
#
# Read-only: never writes or clears the hold (that is check-auth.sh's job).

set -u

HOLD_FILE="${HOLD_FILE:-/downloads/.patreon-dl/.cookie-hold}"
SERVER_MARKER="${SERVER_MARKER:-/tmp/patreon-dl-server-expected}"
SERVER_URL="${SERVER_URL:-http://127.0.0.1:3000}"

if [ -f "$HOLD_FILE" ]; then
  echo "healthcheck: UNHEALTHY — session cookie was rejected; downloads are held." >&2
  echo "             Re-export the full Cookie header from your browser and update config.conf." >&2
  exit 1
fi

if [ -f "$SERVER_MARKER" ]; then
  if ! curl -sf --max-time 5 -o /dev/null "$SERVER_URL"; then
    echo "healthcheck: UNHEALTHY — archive server not responding on ${SERVER_URL}." >&2
    exit 1
  fi
fi

exit 0
