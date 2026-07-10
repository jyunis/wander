# go-pmtiles: the single Go binary that extracts a bbox out of the Protomaps
# planet archive via HTTP range requests (see cairn/regionbuild.py). Pinned to a
# release and verified by digest — this runs server-side, so it is a supply-chain
# dependency of the serving stack, not a convenience script.
FROM debian:bookworm-slim AS pmtiles
ARG PMTILES_VERSION=1.22.3
ARG PMTILES_SHA256=c2ddf8ab9afa2981876dc7e49df0290ed37183570329d3571d1df5f58f66856f
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/pmtiles.tgz \
      "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_x86_64.tar.gz" \
    && echo "${PMTILES_SHA256}  /tmp/pmtiles.tgz" | sha256sum -c - \
    && tar xzf /tmp/pmtiles.tgz -C /usr/local/bin pmtiles \
    && chmod +x /usr/local/bin/pmtiles \
    && /usr/local/bin/pmtiles version


FROM python:3.12-slim AS base

# System deps:
#   - libsqlite3-mod-spatialite: the SpatiaLite loadable extension (FC-6).
#       Provides mod_spatialite.so, loaded at connect by store/repo.py.
#   - tini: PID 1 signal handling so SIGTERM from compose stops cleanly.
#   - ca-certificates, curl: TLS roots + fetching the uv binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libsqlite3-mod-spatialite ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# uv (single static binary) — same toolchain as Ember.
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /uvx /usr/local/bin/

# The region extractor (verified above).
COPY --from=pmtiles /usr/local/bin/pmtiles /usr/local/bin/pmtiles

# Non-root user (uid 1000, matches host ownership of ./data on the volume).
RUN groupadd --gid 1000 cairn \
    && useradd  --uid 1000 --gid 1000 --create-home --shell /bin/bash cairn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    # Where SpatiaLite's loadable module lives on Debian/Ubuntu. store/repo.py
    # falls back to bare "mod_spatialite" if this is unset.
    SPATIALITE_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/mod_spatialite.so

WORKDIR /app

# Install Python deps in their own layer for build cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Project source.
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

# Hand artefacts to the runtime user so `uv run` can write caches in /app.
RUN chown -R 1000:1000 /app

USER 1000:1000

EXPOSE 9931
ENTRYPOINT ["/usr/bin/tini", "--", "uv", "run", "cairn"]
