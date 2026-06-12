FROM node:26-slim

ARG DENO_VERSION=v2.8.2
ARG SUPERCRONIC_VERSION=v0.2.46

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
       ffmpeg \
       curl \
       unzip \
       python3-pip \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp — external downloader for Vimeo/SproutVideo embeds (version tracked in requirements.txt)
COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

# Deno — sandboxes code fetched from YouTube servers during video embed processing
RUN curl -fsSLo /tmp/deno.zip \
    "https://github.com/denoland/deno/releases/download/${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && unzip -d /usr/local/bin /tmp/deno.zip \
    && rm /tmp/deno.zip \
    && chmod +x /usr/local/bin/deno

# supercronic — Docker-friendly cron runner for scheduled sync
RUN curl -fsSLo /usr/local/bin/supercronic \
    "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && chmod +x /usr/local/bin/supercronic

# patreon-dl — installs both `patreon-dl` and `patreon-dl-server` (version tracked in package.json)
WORKDIR /app
COPY package.json ./
RUN npm install
ENV PATH="/app/node_modules/.bin:$PATH"

COPY entrypoint.sh /entrypoint.sh
COPY check-auth.sh /check-auth.sh
COPY healthcheck.sh /healthcheck.sh
RUN chmod +x /entrypoint.sh /check-auth.sh /healthcheck.sh

# Default healthcheck — applies automatically to any container from this image (including
# via compose) with no compose change. Reports unhealthy when the session cookie is held
# (rejected by Patreon; refresh it) or the archive server stops responding. Lenient
# thresholds suit a resource-constrained host; start-period covers slow first server bind.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD /healthcheck.sh

VOLUME ["/config", "/downloads"]
WORKDIR /downloads

ENTRYPOINT ["/entrypoint.sh"]
