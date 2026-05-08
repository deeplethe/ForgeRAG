# ════════════════════════════════════════════════════════════════════════════
# OpenCraig — multi-stage container build.
# Stage 1 builds the Vue frontend; stage 2 is the slim Python runtime
# that serves both the API and the static frontend bundle.
# ════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Vue frontend build ──────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app/web
# Copy lockfile + manifest first so ``npm ci`` is cached when only
# source changes. ``ci`` (not ``install``) so the dependency tree is
# exactly what's pinned — no surprise upgrades in CI.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── Stage 2: Python runtime ──────────────────────────────────────────────
# 3.13-slim matches our dev environment. Pinning to a specific 3.13.x
# isn't necessary — slim tags track the latest patch automatically.
FROM python:3.13-slim

# System deps:
#   * libmupdf-dev / libmupdf-tools — PyMuPDF binding's native dep
#   * gcc / build-essential — wheels that need to compile (psycopg2,
#     some sentence-transformers extras) on glibc-slim images
#   * libpq-dev — psycopg2 client
#   * curl — used by the docker-compose healthcheck and ops scripts
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmupdf-dev libmupdf-tools \
        gcc build-essential libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — copied first so layer cache survives source-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source. Run-time exclusions live in .dockerignore so
# wheels, node_modules, .venv, .git etc. don't get baked into the image.
COPY . .

# Pre-built frontend bundle from stage 1.
COPY --from=frontend /app/web/dist /app/web/dist

# Default storage root — the compose mount overrides this with a named
# volume; for ``docker run`` users without volumes, blobs land here
# inside the container (lost on container removal — the volume mount
# in compose is what makes it persistent).
RUN mkdir -p /app/storage

# Bind to all interfaces inside the container; compose maps the port
# externally. Workers default to 4; tune via OPENCRAIG_WORKERS for
# heavier deployments.
ENV OPENCRAIG_HOST=0.0.0.0 \
    OPENCRAIG_PORT=8000 \
    OPENCRAIG_WORKERS=4

EXPOSE 8000

CMD ["sh", "-c", "python main.py --host \"$OPENCRAIG_HOST\" --port \"$OPENCRAIG_PORT\" --workers \"$OPENCRAIG_WORKERS\""]
