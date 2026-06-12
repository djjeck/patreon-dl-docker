#!/bin/bash
set -e

# Pass-through mode: if arguments are given, run them directly and exit.
# Used for one-shot runs: docker compose run --rm patreon-dl patreon-dl ...
if [ "$#" -gt 0 ]; then
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

# Build the crontab for the scheduled downloader.
# --no-prompt is forced on this unattended flow: cron has no TTY, so patreon-dl's
# confirmation prompt would crash with "ENXIO ... /dev/tty" and the run would exit
# before downloading anything. Forcing it on the CLI overrides whatever no.prompt is
# (or isn't) set to in config.conf, and keeps config.conf.example verbatim upstream.
# Manual one-shot runs go through pass-through mode above and are unaffected.
printf '%s patreon-dl --no-prompt -C /config/config.conf /config/urls.txt\n' \
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

# Start the archive browser
patreon-dl-server -i /downloads &

# Start the scheduled downloader
supercronic /tmp/crontab &

# Exit when either process exits so Docker can restart the container
wait -n
