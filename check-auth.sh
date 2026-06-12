#!/bin/bash
# Preflight authentication check with an on-disk hold.
#
# An expired or malformed cookie does not make patreon-dl fail — it silently falls back
# to downloading whatever Patreon serves anonymous visitors (blurry previews instead of
# patron-only media). With `stop.on = previouslyDownloaded` that degraded content is then
# cached as "downloaded" and never re-fetched, so the failure is both silent and hard to
# recover from. This script validates the configured cookie against Patreon's current_user
# endpoint before any download runs, and records a *hold* when the cookie is rejected so we
# do not hammer Patreon (risking an IP throttle/ban) re-checking a cookie we already know
# is bad.
#
# The hold file (.cookie-hold, next to the browse DB) contains the sha256 of the cookie
# value that was rejected. The cookie is a manually-pasted static snapshot, so its hash
# only changes when the user deliberately re-pastes a fresh cookie. On each run:
#   - hold present, hash == current cookie  -> still the known-bad cookie; skip the network
#                                              call entirely and stay held (exit 4)
#   - hold present, hash != current cookie  -> the cookie changed; clear the hold and
#                                              re-validate
#   - no hold                               -> validate
# A definitive 401 writes the hold; an inconclusive result (network/5xx) never does, so a
# Patreon outage cannot falsely hold.
#
# Exit codes (consumed by entrypoint.sh, the cron wrapper, and healthcheck.sh):
#   0  authenticated        — HTTP 200; safe to download
#   1  expired/invalid      — HTTP 401; hold written
#   2  no cookie configured — check skipped; intentional anonymous use is allowed
#   3  inconclusive         — network error / 5xx / other; skip the run, no hold written
#   4  held                 — known-bad cookie (hold matched); no network call made
#
# Callers treat 1 and 4 identically (skip the download); they differ only for logging.
# The endpoint is overridable via PATREON_AUTH_URL so the test suite can point the check
# at a local mock instead of reaching out to Patreon.

set -u

CONFIG_FILE="${CONFIG_FILE:-/config/config.conf}"
AUTH_URL="${PATREON_AUTH_URL:-https://www.patreon.com/api/current_user}"
HOLD_FILE="${HOLD_FILE:-/downloads/.patreon-dl/.cookie-hold}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "auth-check: no config at ${CONFIG_FILE} — skipping (anonymous use)." >&2
  exit 2
fi

# Extract the cookie value exactly as the user wrote it: take the right-hand side of the
# first `cookie =` line, stripping an inline comment and surrounding whitespace. The value
# is the full Cookie header (name=value; name=value; ...), sent verbatim — we do not
# reconstruct or parse individual cookies.
COOKIE=$(grep -E '^\s*cookie\s*=' "$CONFIG_FILE" \
  | sed 's/[[:space:]]*#.*$//; s/^[^=]*=[[:space:]]*//' \
  | head -1)

if [ -z "$COOKIE" ]; then
  echo "auth-check: no cookie configured — skipping (anonymous use)." >&2
  exit 2
fi

COOKIE_HASH=$(printf '%s' "$COOKIE" | sha256sum | cut -d' ' -f1)

# Honour an existing hold. If it matches the current cookie, the cookie is still the one
# we already know is bad — stay held without contacting Patreon. If it differs, the cookie
# has changed; clear the hold and fall through to re-validate.
if [ -f "$HOLD_FILE" ]; then
  if [ "$(cat "$HOLD_FILE" 2>/dev/null)" = "$COOKIE_HASH" ]; then
    echo "auth-check: cookie is held (previously rejected, unchanged) — skipping check." >&2
    exit 4
  fi
  echo "auth-check: cookie changed since the last failure — clearing hold and re-checking." >&2
  rm -f "$HOLD_FILE"
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
    echo "auth-check: cookie is expired or invalid (HTTP 401) — holding." >&2
    echo "            Re-export the full Cookie header from your browser and update ${CONFIG_FILE}." >&2
    mkdir -p "$(dirname "$HOLD_FILE")"
    printf '%s\n' "$COOKIE_HASH" > "$HOLD_FILE"
    exit 1
    ;;
  *)
    echo "auth-check: unexpected response (HTTP ${HTTP}) — inconclusive." >&2
    exit 3
    ;;
esac
