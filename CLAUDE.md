# patreon-dl-docker

Docker image wrapping [patreon-dl](https://github.com/patrickkfkan/patreon-dl) with all required runtime dependencies.

## Image versioning

Image tags track the `patreon-dl` npm package version (e.g., `3.9.0`). Every push to `main` publishes
all versioned tags — e.g. `x.y.z`, `x.y.z-YYYYMMDD`, `x.y`, and `latest` — in addition to `edge` and
`edge-YYYYMMDD`. This means `main` must always be in a shippable state (see Branch discipline below).

The date suffix (`-YYYYMMDD`) allows multiple container releases within the same upstream version —
e.g. when a dependency bump or entrypoint fix warrants a new image without a new `patreon-dl` release.
Users are encouraged to pin to the bare version tag (e.g. `3.9.0`), which always points to the latest
container build for that upstream version.

The full automated flow when `patreon-dl` releases a new version:

1. Dependabot opens a PR bumping `patreon-dl` in `package.json`
2. PR is merged to `main`
3. `auto-tag.yml` fetches `example.conf` from the upstream patreon-dl repo at the new version tag
4. `auto-tag.yml` commits the updated `config/config.conf.example` to `main` (if changed)
5. `auto-tag.yml` creates and pushes `v3.10.0` at the resulting HEAD
6. `build-push.yml` fires on the tag push, verifies `config/config.conf.example` matches upstream,
   then publishes all tags to GHCR

**Requires a `CONTENTS_PAT` repo secret** — a fine-grained PAT with "Contents: write" on this repo.
Without it the tag push uses `GITHUB_TOKEN` and does not trigger `build-push.yml` automatically.
Create one at GitHub → Settings → Developer settings → Fine-grained personal access tokens.

`yt-dlp` bumps (via `requirements.txt`) follow the same Dependabot flow but only rebuild the image
under the current version tags. They do not bump the version number.

**Deno and supercronic** are not tracked by Dependabot. To update them manually:

1. Check their latest releases (links in the Dependencies table below)
2. Update the relevant `ARG` in `Dockerfile`
3. Commit and merge to `main` — all version tags are republished with the updated image

## Branch discipline

`main` is always shippable. Every push to `main` republishes `latest`, `x.y.z`, and `x.y` — there
is no pre-release state on this branch.

**Do all work-in-progress on a feature branch.** Only merge to `main` when the change is complete
and tested. This applies to all changes: dependency bumps, entrypoint fixes, CI changes, and
Dockerfile updates.

## Entrypoint behaviour

**Pass-through mode** — if arguments are passed to the container, `exec "$@"` runs them directly and exits. Used for one-shot CLI runs: `docker compose run --rm patreon-dl patreon-dl ...`. No server, no cron. If the command is a downloading `patreon-dl` invocation (`is_downloading_command`), the preflight auth check (`require_auth`) runs first and hard-refuses on an expired or unverifiable cookie — see below. Read-only invocations (`-h`, `--help`, `--dry-run`, `--list-tiers`, `--list-tiers-uid`) and any non-`patreon-dl` command (e.g. `patreon-dl-server`, `yt-dlp`, a shell) are not guarded.

**Normal mode** (no arguments) — validates config, then branches on whether the browse DB exists:

- **DB missing** (`/downloads/.patreon-dl/db.sqlite` not found): prints a message guiding the user to run the downloader manually first, then `exec supercronic` (scheduler only, no server). The container stays alive; the server starts on the next restart after the first download creates the DB.
- **DB present**: starts both `patreon-dl-server -i /downloads` (background) and `supercronic` (background), then `wait -n` — exits when either process exits so Docker can restart the container.

The entrypoint also validates `out.dir` in `config.conf` and exits with an error if it is set to anything other than `/downloads`.

## Preflight authentication check

An expired or malformed cookie does not make patreon-dl error — it silently downloads
whatever Patreon serves anonymous visitors (blurry previews) and, with
`stop.on = previouslyDownloaded`, caches that degraded content as "done" so it is never
re-fetched. `check-auth.sh` guards against this by validating the configured cookie
against Patreon's `current_user` endpoint before any download runs. It maps the result
to exit codes:

| Exit | Meaning              | Trigger                | Manual flow & boot    | Cron flow       |
| ---- | -------------------- | ---------------------- | --------------------- | --------------- |
| 0    | Authenticated        | HTTP 200               | Proceed               | Run downloader  |
| 1    | Expired/invalid      | HTTP 401               | Refuse (boot: crash)  | Skip run        |
| 2    | No cookie configured | `cookie =` unset/empty | Proceed (anonymous)   | Run (anonymous) |
| 3    | Inconclusive         | network error / 5xx    | Refuse — except boot¹ | Skip run        |

¹ The boot check treats exit 3 as non-fatal so a transient blip at startup doesn't
crash-loop the container; the per-run cron check re-verifies before any download.

The check runs in **three places**:

- **Manual / pass-through flow** (`require_auth` in `entrypoint.sh`): guards the
  interactive download flow, including the first backfill. A human is present, so it
  hard-refuses (exit 1) on both an expired cookie (1) and an unverifiable result (3) —
  better than silently downloading degraded content. Proceeds only on 0 or 2.
- **At boot** (normal mode): on exit 1 the container exits 1 and crash-loops under
  `restart: unless-stopped`, making an expired cookie loudly visible. Exit 2/3 do not
  block startup. _(Known limitation: crashing at boot also takes the archive browser
  down even though browsing an existing archive needs no auth. Accepted for now; see
  issue #7 for the planned iteration.)_
- **Per scheduled run** (in the generated crontab): each cron tick runs `check-auth.sh`
  first and only invokes the downloader on exit 0 or 2. Unattended, so on 1 or 3 it
  skips the run (and logs) rather than crashing. This is what catches a cookie that
  expires _while the container is already running_ — the boot check cannot.

The endpoint is overridable via the `PATREON_AUTH_URL` env var so the test suite can
point the check at a local mock and stay hermetic (it never reaches Patreon). The cookie
is read verbatim from the `cookie =` line — it must be the full `Cookie` header, not just
`session_id` (see README step 1).

The scheduled (cron) downloader is invoked with `--no-prompt` forced on the CLI. Cron has no TTY, so patreon-dl's confirmation prompt would crash (`ENXIO ... /dev/tty`) and the run would exit before downloading anything. Forcing the flag here overrides whatever `no.prompt` is set to in `config.conf` — which keeps `config.conf.example` verbatim upstream rather than hardcoding a value into the synced example file. Manual one-shot runs go through pass-through mode and are unaffected (pass `--no-prompt` yourself if you want it).

## Dependencies

| Tool        | Why it's needed                                  | Version tracking                                         |
| ----------- | ------------------------------------------------ | -------------------------------------------------------- |
| patreon-dl  | Downloader + archive server                      | Dependabot (npm / `package.json`)                        |
| yt-dlp      | External video embeds (Vimeo, SproutVideo)       | Dependabot (pip / `requirements.txt`)                    |
| Deno        | Sandboxes code from YouTube servers              | Manual — https://github.com/denoland/deno/releases       |
| FFmpeg      | Streaming-format video and audio processing      | Manual — via apt (`ffmpeg` package)                      |
| supercronic | Docker-friendly cron — handles signals correctly | Manual — https://github.com/aptible/supercronic/releases |

## Extending the example file sync

`auto-tag.yml` and `build-push.yml` each contain a `fetch_upstream` / `verify_upstream` function.
To sync an additional file from upstream, add one call to each function.

`config/urls.txt.example` is our own creation — upstream has no equivalent. If upstream ever adds one,
add it to both functions to include it in the automated sync.

## Testing a build locally

The canonical entrypoint is the `Makefile` — no manual setup, no global `pip install`:

```sh
make test        # set up the venv, build the image (live build log), then run the suite
make test-fast   # run the suite against an already-built image (skips the rebuild)
make build       # just build the test image
make clean       # remove the test image
make clean-venv  # remove the .venv test virtualenv
make help        # list targets
```

`make test` manages everything itself:

1. Creates a `.venv/` (gitignored) and installs `requirements-test.txt` into it. This step is
   skipped on subsequent runs unless `requirements-test.txt` changes, and it sidesteps PEP 668
   (externally-managed Python) since a venv is never externally managed.
2. Builds the image with `docker build`, so the **build log is visible live in your terminal** —
   the build takes several minutes on a cold cache (apt, npm, Deno, supercronic).
3. Runs pytest with `TEST_IMAGE_TAG` set, so pytest reuses that image instead of building one
   itself.

Iterate on tests with `make test-fast` (reuses the existing image).

The suite never builds the image itself — building is owned by the Makefile and by CI, which both
set `TEST_IMAGE_TAG` to the image they built. Running bare `pytest` without `TEST_IMAGE_TAG` exits
immediately with guidance to run `make test`, rather than silently building for several minutes.

The test suite covers entrypoint behaviour only — tool smoke tests, pass-through mode,
`out.dir` validation, the no-DB guidance branch, the DB-present server start, and the
preflight auth-check exit codes (via a local mock endpoint on a throwaway Docker network,
so it never reaches Patreon). It does not connect to Patreon or test patreon-dl's download
logic.

## Upstream references

- patreon-dl GitHub: https://github.com/patrickkfkan/patreon-dl
- patreon-dl example.conf: https://github.com/patrickkfkan/patreon-dl/blob/master/example.conf
- patreon-dl releases: https://github.com/patrickkfkan/patreon-dl/releases
