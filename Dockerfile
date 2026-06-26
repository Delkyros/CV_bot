# syntax=docker/dockerfile:1
#
# Hardened, multi-stage image for the JobMatch AI batch pipeline.
# Goals: small attack surface, no build tools in the final image, runs as a
# non-root user, and is friendly to a read-only root filesystem (see
# docker-compose.example.yml).
#
# Tip: for maximum reproducibility/security, pin the base image by digest, e.g.
#   FROM python:3.10-slim-bookworm@sha256:<digest> AS builder

############################################################
# Stage 1 — builder: install deps into an isolated virtualenv
############################################################
FROM python:3.10-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Self-contained venv we can copy wholesale into the clean runtime image.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

############################################################
# Stage 2 — runtime: minimal image with only the venv + app
############################################################
FROM python:3.10-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# No apt packages are installed in this stage (the app needs only Python deps,
# already baked into /opt/venv), so the attack surface stays minimal. For OS
# security patches, pull a fresh `python:3.10-slim-bookworm` base and rebuild —
# or pin the base by digest (see the top of this file). Running `apt-get
# upgrade` at build time is intentionally avoided: it makes builds
# non-reproducible and can corrupt the package DB.

# Non-root user with a fixed UID/GID for predictable volume ownership.
RUN groupadd --gid 10001 appgroup \
 && useradd --uid 10001 --gid appgroup --create-home --shell /usr/sbin/nologin appuser

# Bring in the prebuilt virtualenv (no build tooling comes along).
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy only what the app needs at runtime; config/ and data/ are mounted.
COPY --chown=appuser:appgroup main.py webapp.py ./
COPY --chown=appuser:appgroup src/ ./src/
COPY --chown=appuser:appgroup web/ ./web/
COPY --chown=appuser:appgroup docker-entrypoint.sh ./

# Normalize line endings (in case of a CRLF checkout on Windows) and make the
# scheduler entrypoint executable.
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh \
 && chmod +x /app/docker-entrypoint.sh \
 && mkdir -p /app/data && chown appuser:appgroup /app/data

USER appuser

# Scheduler entrypoint: runs `python main.py` every RUN_INTERVAL_SECONDS
# (default 6h). Set RUN_INTERVAL_SECONDS=0 to run once and exit.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
