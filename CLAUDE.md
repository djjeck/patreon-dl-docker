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

**Pass-through mode** — if arguments are passed to the container, `exec "$@"` runs them directly and exits. Used for one-shot CLI runs: `docker compose run --rm patreon-dl patreon-dl ...`. No server, no cron.

**Normal mode** (no arguments) — validates config, then branches on whether the browse DB exists:

- **DB missing** (`/downloads/.patreon-dl/db.sqlite` not found): prints a message guiding the user to run the downloader manually first, then `exec supercronic` (scheduler only, no server). The container stays alive; the server starts on the next restart after the first download creates the DB.
- **DB present**: starts both `patreon-dl-server -i /downloads` (background) and `supercronic` (background), then `wait -n` — exits when either process exits so Docker can restart the container.

The entrypoint also validates `out.dir` in `config.conf` and exits with an error if it is set to anything other than `/downloads`.

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

Run the integration test suite (builds the image automatically if `TEST_IMAGE_TAG` is not set):

```sh
pip install -r requirements-test.txt
pytest -v
```

To skip the image build (e.g. after `docker build -t myimage:local .`):

```sh
TEST_IMAGE_TAG=myimage:local pytest -v
```

The test suite covers entrypoint behaviour only — tool smoke tests, pass-through mode,
`out.dir` validation, the no-DB guidance branch, and the DB-present server start. It
does not connect to Patreon or test patreon-dl's download logic.

## Upstream references

- patreon-dl GitHub: https://github.com/patrickkfkan/patreon-dl
- patreon-dl example.conf: https://github.com/patrickkfkan/patreon-dl/blob/master/example.conf
- patreon-dl releases: https://github.com/patrickkfkan/patreon-dl/releases
