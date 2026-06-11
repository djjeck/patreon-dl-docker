# patreon-dl-docker

Docker image wrapping [patreon-dl](https://github.com/patrickkfkan/patreon-dl) with all required runtime dependencies.

## Image versioning

Image tags track the `patreon-dl` npm package version (e.g., `3.9.0`). The full automated flow:

1. Dependabot opens a PR bumping `patreon-dl` in `package.json`
2. PR is merged to `main`
3. `auto-tag.yml` fetches `example.conf` from the upstream patreon-dl repo at the new version tag
4. `auto-tag.yml` commits the updated `config/config.conf.example` to `main` (if changed)
5. `auto-tag.yml` creates and pushes `v3.10.0` at the resulting HEAD
6. `build-push.yml` fires on the tag, verifies `config/config.conf.example` matches upstream, then publishes `3.10.0`, `3.10`, and `latest` to GHCR

**Requires a `CONTENTS_PAT` repo secret** — a fine-grained PAT with "Contents: write" on this repo. Without it the tag push uses `GITHUB_TOKEN` and does not trigger `build-push.yml` automatically. Create one at GitHub → Settings → Developer settings → Fine-grained personal access tokens.

`yt-dlp` bumps (via `requirements.txt`) follow the same Dependabot flow but only rebuild the `edge` tag. They are bundled into the image on the next patreon-dl release.

**Deno and supercronic** are not tracked by Dependabot. To update them manually:

1. Check their latest releases (links in the Dependencies table below)
2. Update the relevant `ARG` in `Dockerfile`
3. Commit and merge — `edge` updates, and the new version is included in the next patreon-dl release tag

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

```sh
docker build -t patreon-dl-docker:local .
docker run --rm patreon-dl-docker:local patreon-dl --version
docker run --rm patreon-dl-docker:local yt-dlp --version
docker run --rm patreon-dl-docker:local deno --version
docker run --rm patreon-dl-docker:local supercronic --version
```

## Upstream references

- patreon-dl GitHub: https://github.com/patrickkfkan/patreon-dl
- patreon-dl example.conf: https://github.com/patrickkfkan/patreon-dl/blob/master/example.conf
- patreon-dl releases: https://github.com/patrickkfkan/patreon-dl/releases
