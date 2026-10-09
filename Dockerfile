# huvu and Hermes in one image, built on Hermes's official one, so Hermes's
# runtime stays as Nous packages it. A slim image with Hermes installed
# from source was tried on 2026-10-08 and dropped: Hermes's package manager
# didn't recognize the install and built its own runtime without the API
# server's extras.
#
# Hermes's gateway is the container's main process, as in its own image,
# and huvu's backend runs beside it as an s6 service (docker/s6/huvu). On
# every start, docker/cont-init/50-huvu writes huvu's skills and runs
# setup.sh, so the default profile is always set up for huvu.
#
# Everything kept lives on one volume at /opt/data: Hermes's profiles and
# logins, and huvu's database, config and secrets under /opt/data/huvu.
# See compose.yaml for how to run it, and hrishik.md for the first start.
#
# The backend also serves the web client, built in the first stage, so
# https://huvu.hrishik.me is both the app and its backend.
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN VITE_TARGET=web npm run build:web

# Hermes is pinned to the image huvu was last checked with (2026-09-24's
# latest). The one after it (2026-10-08) shipped without uv and without
# aiohttp, so its API server never started. Move the pin on purpose, and
# check an ask end to end after.
FROM nousresearch/hermes-agent@sha256:d4da4a40cd7a28aba983775d9fd31d94cbf153eeb0cb9e844d6d0f612b7c24db

USER root

ENV HUVU_CONFIG=/opt/data/huvu/huvu.toml \
    HUVU_ENV_FILE=/opt/data/huvu/.env \
    HUVU_WEB_DIR=/opt/huvu/web \
    UV_CACHE_DIR=/tmp/uv-cache

# uv comes from Astral's own image, at the version the lock file was written
# with, so the build doesn't hang on Hermes shipping one (2026-10-08's didn't).
COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /uvx /usr/local/bin/

WORKDIR /opt/huvu
COPY --chown=hermes:hermes backend/ backend/
RUN cd backend && uv sync -q --frozen --no-dev && chown -R hermes:hermes .venv
COPY --chown=hermes:hermes hermes-plugin/ hermes-plugin/
COPY --chown=hermes:hermes SOUL.md setup.sh wipe.sh ./
COPY --chown=hermes:hermes docker/huvu.toml docker/huvu.toml
COPY --from=web /web/dist web/
COPY docker/s6/ /etc/s6-overlay/s6-rc.d/
COPY docker/cont-init/ /etc/cont-init.d/
RUN chmod +x setup.sh wipe.sh /etc/cont-init.d/50-huvu /etc/s6-overlay/s6-rc.d/huvu/run && \
    install -d -o hermes -g hermes hermes-skills

EXPOSE 3001
CMD ["gateway", "run"]
