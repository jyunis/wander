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
