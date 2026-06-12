# patreon-dl-docker

Docker image for [patreon-dl](https://github.com/patrickkfkan/patreon-dl) — a tool for archiving Patreon content (posts, media, attachments, shop products) using your account session cookie.

Published to: `ghcr.io/djjeck/patreon-dl-docker`

## Quick start

Save the following as `compose.yml` in a new directory:

```yaml
services:
  patreon-dl:
    # Pin to a specific tag in production (e.g. ghcr.io/djjeck/patreon-dl-docker:3.9.0).
    # See https://github.com/djjeck/patreon-dl-docker/releases for available tags.
    image: ghcr.io/djjeck/patreon-dl-docker:latest
    container_name: patreon-dl
    restart: unless-stopped
    # Do NOT add a `healthcheck:` here. The image ships a default healthcheck that
    # reports unhealthy when your session cookie expires; overriding it loses that
    # signal. See the Healthcheck section below.
    environment:
      - TZ=UTC
      - CRON_SCHEDULE=0 3 * * * # 03:00 daily (uses the time-zone above)
    ports:
      - "3000:3000"
    volumes:
      - ./config:/config:ro
      - ./downloads:/downloads
      # If ./downloads is on network storage (NFS/CIFS), mount the archive browser
      # database on local disk to avoid SQLite file-locking errors (see note below):
      # - patreon-dl-db:/downloads/.patreon-dl
      # Optional: expose logs on the host:
      # - ./logs:/downloads/logs

# volumes:
#   patreon-dl-db:
```

> **Network storage**: The archive browser stores its database at `/downloads/.patreon-dl/db.sqlite`. SQLite file locking is unreliable on network filesystems (NFS, CIFS, SMB) and will fail with `database is locked`. If `./downloads` points to a network share, uncomment the `patreon-dl-db` named volume above to keep the database on local disk.

> **Not using Compose?** Replace `docker compose run --rm patreon-dl` in the steps below with:
>
> ```
> docker run --rm -v ./config:/config:ro -v ./downloads:/downloads ghcr.io/djjeck/patreon-dl-docker:latest
> ```
>
> For step 4, use `docker run -d` with the same flags, plus `--name patreon-dl --restart unless-stopped -e TZ=UTC -e CRON_SCHEDULE="0 3 * * *" -p 3000:3000`.

### 1. Get your Patreon cookie

Log in to [patreon.com](https://patreon.com) in your browser, then copy the **full `Cookie` request header** — not just the `session_id` value. The easiest way: open your browser's developer tools → Network tab, reload patreon.com, click any request to `patreon.com`, and under **Request Headers** copy the entire `Cookie:` value (a string of `name=value; name=value; …` pairs). See the upstream guide: [How to obtain Cookie](https://github.com/patrickkfkan/patreon-dl/wiki/How-to-obtain-Cookie).

> Use the complete header, not just `session_id`. If the cookie is incomplete or expired the container pauses downloads and reports unhealthy rather than fetching the unauthenticated public version of your content — see [Cookie validation](#cookie-validation) below.

### 2. Create your config

```sh
cp config/config.conf.example config/config.conf
cp config/urls.txt.example config/urls.txt
```

Edit `config/config.conf` — at minimum set these two values:

- `cookie` — your full Patreon `Cookie` header (see step 1; required for patron-only content)
- `stop.on = previouslyDownloaded` — so each run only processes new posts (see step 4)

`out.dir` must not be set — downloads always go to `/downloads` (the fixed output directory), which the archive server reads from. Setting it to a different path will cause a startup error.

Edit `config/urls.txt` — add one `https://www.patreon.com/<creator>/posts` URL per line.

### 3. Run a one-time backfill

```sh
docker compose run --rm patreon-dl patreon-dl -C /config/config.conf /config/urls.txt
```

This runs **only the `patreon-dl` downloader CLI** and exits — no archive server, no cron scheduler. It is different from normal operation (step 4), which starts both. Run creators sequentially to avoid session invalidation from parallel requests.

### 4. Enable ongoing sync

The `patreon-dl` service in `docker-compose.yml` runs automatically on a cron schedule (default: 03:00 daily). With `stop.on = previouslyDownloaded` in your config, each run stops when it reaches already-archived posts, so only new content is downloaded.

```sh
docker compose up -d patreon-dl
```

### 5. Browse the archive

Open `http://localhost:3000` — the archive browser starts automatically alongside the downloader. **It has no built-in authentication** — keep it on a trusted network or behind an authenticating reverse proxy.

## Configuration reference

[`config/config.conf.example`](config/config.conf.example) is the verbatim upstream [`example.conf`](https://github.com/patrickkfkan/patreon-dl/blob/master/example.conf). All options are documented in-file.

> **Note:** the scheduled (cron) downloader always runs with `--no-prompt` forced on, regardless of the `no.prompt` value in your `config.conf`. Cron has no TTY, so leaving the confirmation prompt enabled would make every scheduled run crash before downloading. This only affects the cron flow — manual one-shot runs (below) honour whatever you set on the config file.

## Cookie validation

An expired or malformed cookie does **not** make patreon-dl fail — it silently downloads the blurry public-preview version of your patron-only content, and (with `stop.on = previouslyDownloaded`) caches it as already-downloaded so it is never re-fetched. To catch this, the container validates your cookie against Patreon before downloading:

- **Manual one-shot downloads** (`docker compose run --rm patreon-dl patreon-dl ...`, including the first backfill in step 3): the check runs before the download starts. If the cookie is expired/invalid — or can't be verified — the command refuses to run and exits non-zero. `--dry-run`, `--list-tiers`, and `-h` are not guarded (they write no content).
- **The ongoing-sync container** keeps running even with a bad cookie, so the archive browser stays available. When the cookie is rejected, the container **reports unhealthy** (visible in `docker ps`) and pauses downloads — this is your signal to refresh the cookie. It does **not** crash-loop.
- **Before each scheduled run**, the cookie is re-checked; a rejected cookie skips that run (and is logged) rather than downloading degraded content.
- If no `cookie` is configured, the check is skipped so intentional public-content downloads work.

**Recovering from an expired cookie:** re-export the full `Cookie` header (see step 1) and update `config.conf`. The next scheduled run (or a container restart) detects the changed cookie, clears the pause, re-validates, and the container returns to healthy — no manual intervention beyond updating the config. While a cookie is paused, the container does **not** re-contact Patreon for it, so a dead cookie cannot get your IP throttled.

A transient network error or Patreon outage is treated as inconclusive — it does not pause downloads; a scheduled run is skipped and retried on the next tick, and a manual run refuses rather than risk downloading degraded content.

### Healthcheck

The image ships a default `HEALTHCHECK` (no Compose change needed). The container is **unhealthy** when the cookie is paused (see above) or the archive browser stops responding on port 3000; otherwise **healthy**. The reason is written to the container logs. You can watch it with `docker inspect --format '{{.State.Health.Status}}' patreon-dl` or in `docker ps`.

## Environment variables

| Variable        | Default     | Description                        |
| --------------- | ----------- | ---------------------------------- |
| `CRON_SCHEDULE` | `0 3 * * *` | Cron expression for scheduled sync |

## Running manually (one-shot)

Pass a command after the service name to run it directly inside the container, bypassing the normal entrypoint (no archive server, no cron scheduler):

```sh
# Download all creators in urls.txt
docker compose run --rm patreon-dl patreon-dl -C /config/config.conf /config/urls.txt

# Dry run (no files written)
docker compose run --rm patreon-dl patreon-dl -C /config/config.conf --dry-run /config/urls.txt

# Single creator
docker compose run --rm patreon-dl patreon-dl -C /config/config.conf https://www.patreon.com/creatorname/posts
```

## What's included

| Tool                                                     | Purpose                                     | Version source                                               |
| -------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------ |
| [patreon-dl](https://github.com/patrickkfkan/patreon-dl) | Downloader + archive server                 | [`package.json`](package.json) — Dependabot (daily)          |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp)               | External video embeds (Vimeo, SproutVideo)  | [`requirements.txt`](requirements.txt) — Dependabot (weekly) |
| [Deno](https://deno.com)                                 | Sandboxes code fetched from YouTube servers | [`Dockerfile`](Dockerfile) — manual                          |
| [FFmpeg](https://ffmpeg.org)                             | Streaming-format video processing           | system apt package                                           |
| [supercronic](https://github.com/aptible/supercronic)    | Docker-friendly cron runner                 | [`Dockerfile`](Dockerfile) — manual                          |

## Image tags

Image tags track the patreon-dl version: `3.x.y` and `3.x` tags are published for each release, plus `latest` (most recent release) and `edge` (most recent commit to `main`).

**Pin to a specific tag in production.** `latest` is convenient for getting started but will update automatically as new patreon-dl versions are released.

## Known limitations

- **DRM-protected videos**: some Patreon-hosted videos are DRM-protected and will be skipped by patreon-dl. Gaps in the archive for these are expected.
- **Session expiry**: the session cookie expires periodically. When patreon-dl starts failing with authentication errors, re-export the full `Cookie` header from your browser (see step 1) and update `config.conf`.
- **Rate limiting**: during large initial backfills, run creators sequentially. Parallelizing requests increases the risk of session invalidation.

## License

MIT. patreon-dl itself is [MIT licensed](https://github.com/patrickkfkan/patreon-dl/blob/master/LICENSE).
