#!/bin/bash
set -e

# Run the preflight auth check and act on the result as a present, interactive user:
# proceed only when the cookie is valid (0) or intentionally absent (2); hard-refuse on
# an expired/held cookie (1/4) or an inconclusive result (3). Used only for the manual
# pass-through flow — a human is watching a one-shot run, so a hard stop beats silently
# downloading degraded content. (The boot and cron flows do not refuse; they keep the
# container up and let the hold + healthcheck signal the problem.)
require_auth() {
  local rc=0
  /check-auth.sh || rc=$?
  case "$rc" in
    0|2) return 0 ;;
    1|4)
      echo "patreon-dl: refusing to run — session cookie is expired or invalid." >&2
      exit 1
      ;;
    *)
      echo "patreon-dl: refusing to run — could not verify the session cookie (auth check returned $rc)." >&2
      exit 1
      ;;
  esac
}

# True when the given command is a patreon-dl invocation that actually downloads content.
# Read-only / no-write invocations (help, tier listing, dry runs) and any other command
# (patreon-dl-server, yt-dlp, a shell, ...) need no auth guard.
is_downloading_command() {
  [ "$1" = "patreon-dl" ] || return 1
  local arg
  for arg in "$@"; do
    case "$arg" in
      -h|--help|--dry-run|--list-tiers|--list-tiers-uid) return 1 ;;
    esac
  done
  return 0
}

# Pass-through mode: if arguments are given, run them directly and exit.
# Used for one-shot runs: docker compose run --rm patreon-dl patreon-dl ...
# Guard the interactive download flow the same way as scheduled runs — an expired or
# malformed cookie is just as harmful here (it pollutes the status cache with degraded
# public-preview content), even though patreon-dl shows a confirmation prompt.
if [ "$#" -gt 0 ]; then
  if is_downloading_command "$@"; then
    require_auth
  fi
  exec "$@"
fi

# Normal operation.

# Reject an out.dir setting that would send downloads somewhere the server cannot find.
# The server always reads from /downloads, so both must agree.
if [ -f /config/config.conf ]; then
  OUT_DIR=$(grep -E '^\s*out\.dir\s*=' /config/config.conf \
    | sed 's/[[:space:]]*#.*$//; s/.*=[[:space:]]*//' \
    | tr -d ' ' \
    | head -1)
  if [ -n "$OUT_DIR" ] && [ "$OUT_DIR" != "/downloads" ] && [ "$OUT_DIR" != "/downloads/" ]; then
    echo "ERROR: out.dir in config.conf is set to '${OUT_DIR}'." >&2
    echo "       This image only supports /downloads. Remove the out.dir setting." >&2
    exit 1
  fi
fi

# Preflight auth check at boot. This never crashes the container: a bad cookie writes a
# hold (in check-auth.sh) and the default healthcheck turns the container unhealthy, which
# signals the user to refresh the cookie while the archive UI stays up. A hold also stops
# the boot check from re-contacting Patreon for a cookie already known to be bad. The
# result here is informational — the per-run cron check and the healthcheck enforce it.
# `|| true` keeps any non-zero exit from tripping `set -e`.
/check-auth.sh || true

# Build the crontab for the scheduled downloader.
# Each scheduled run is gated by check-auth.sh: the downloader runs only when the cookie
# is valid (exit 0) or intentionally absent (exit 2). On an expired/held cookie (exit 1/4)
# or an inconclusive result (exit 3) the run is skipped rather than allowed to download
# degraded content — the cron entry still exits 0 so supercronic does not flag a job
# failure. This per-run check catches a cookie that expires while the container is already
# running; it also re-validates after the user re-pastes a cookie (check-auth.sh clears
# the hold when the cookie hash changes), which is what brings the container back to
# healthy without a restart.
#
# --no-prompt is forced on this unattended flow: cron has no TTY, so patreon-dl's
# confirmation prompt would crash with "ENXIO ... /dev/tty" and the run would exit
# before downloading anything. Forcing it on the CLI overrides whatever no.prompt is
# (or isn't) set to in config.conf, and keeps config.conf.example verbatim upstream.
# Manual one-shot runs go through pass-through mode above and are unaffected.
printf '%s /check-auth.sh; rc=$?; if [ "$rc" = 0 ] || [ "$rc" = 2 ]; then patreon-dl --no-prompt -C /config/config.conf /config/urls.txt; else echo "scheduled run skipped: auth check returned $rc"; fi\n' \
  "${CRON_SCHEDULE:-0 3 * * *}" > /tmp/crontab

# If the browse DB does not exist yet, the archive server cannot start — it performs
# a hard existence check before opening the file. Guide the user to run the downloader
# manually first, then start only the scheduler so the container stays alive.
DB_FILE="/downloads/.patreon-dl/db.sqlite"
if [ ! -f "$DB_FILE" ]; then
  cat >&2 <<EOF

patreon-dl: archive browser not started — no database found at ${DB_FILE}

The archive server requires an existing database. On a fresh install, run the
downloader manually at least once before starting the container:

    docker compose run --rm patreon-dl patreon-dl -C /config/config.conf /config/urls.txt

If you have multiple creators in urls.txt, run them one at a time to avoid
rate-limiting your Patreon session.

Once the first download completes, restart the container to bring up the archive browser.

In case you really don't want to do that, the scheduled downloader is starting now
(CRON_SCHEDULE: ${CRON_SCHEDULE:-0 3 * * *}).
After its first run, restart the container to bring up the archive browser too.

EOF
  exec supercronic /tmp/crontab
fi

# Mark that the archive server is expected to be running, so the healthcheck knows to
# probe :3000 (the no-DB branch above exits before here and never sets this, so the
# healthcheck skips the server probe in scheduler-only mode). The marker lives in /tmp
# so it resets on every container start.
touch /tmp/patreon-dl-server-expected

# Start the archive browser
patreon-dl-server -i /downloads &

# Start the scheduled downloader
supercronic /tmp/crontab &

# Exit when either process exits so Docker can restart the container
wait -n
