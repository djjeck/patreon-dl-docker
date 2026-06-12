#!/bin/bash
# Preflight authentication check.
#
# An expired or malformed `session_id` does not make patreon-dl fail — it silently
# falls back to downloading whatever Patreon serves to anonymous visitors (blurry
# previews instead of patron-only media). With `stop.on = previouslyDownloaded` that
# degraded content is then cached as "downloaded" and never re-fetched, so the failure
# is both silent and hard to recover from. This script validates the configured cookie
# against Patreon's current_user endpoint before any download is allowed to run.
#
# Exit codes (consumed by entrypoint.sh and the cron wrapper):
#   0  authenticated        — HTTP 200; safe to download
#   1  expired/invalid      — HTTP 401; the cookie no longer authenticates
#   2  no cookie configured — check skipped; intentional anonymous use is allowed
#   3  inconclusive         — network error, 5xx, or any other response; cannot
#                             confirm auth, so callers should skip the run rather
#                             than risk downloading degraded content
#
# The endpoint is overridable via PATREON_AUTH_URL so the test suite can point the
# check at a local mock instead of reaching out to Patreon.

set -u

CONFIG_FILE="${CONFIG_FILE:-/config/config.conf}"
AUTH_URL="${PATREON_AUTH_URL:-https://www.patreon.com/api/current_user}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "auth-check: no config at ${CONFIG_FILE} — skipping (anonymous use)." >&2
  exit 2
fi

# Extract the cookie value exactly as the user wrote it: take the right-hand side of
# the first `cookie =` line, stripping an inline comment and surrounding whitespace.
# The value is the full Cookie header (name=value; name=value; ...), so it is sent
# verbatim — we do not reconstruct or parse individual cookies.
COOKIE=$(grep -E '^\s*cookie\s*=' "$CONFIG_FILE" \
  | sed 's/[[:space:]]*#.*$//; s/^[^=]*=[[:space:]]*//' \
  | head -1)

if [ -z "$COOKIE" ]; then
  echo "auth-check: no cookie configured — skipping (anonymous use)." >&2
  exit 2
fi

HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
  -H "Cookie: ${COOKIE}" "$AUTH_URL")
CURL_RC=$?

if [ "$CURL_RC" -ne 0 ]; then
  echo "auth-check: could not reach ${AUTH_URL} (curl exit ${CURL_RC}) — inconclusive." >&2
  exit 3
fi

case "$HTTP" in
  200)
    echo "auth-check: cookie is valid (HTTP 200)." >&2
    exit 0
    ;;
  401)
    echo "auth-check: cookie is expired or invalid (HTTP 401)." >&2
    echo "            Re-export the full Cookie header from your browser and update ${CONFIG_FILE}." >&2
    exit 1
    ;;
  *)
    echo "auth-check: unexpected response (HTTP ${HTTP}) — inconclusive." >&2
    exit 3
    ;;
esac
